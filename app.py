import json
import os

from flask import Flask, request, jsonify, render_template, send_from_directory
from groq import APIConnectionError, AuthenticationError, Groq, RateLimitError
from werkzeug.exceptions import RequestEntityTooLarge

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.config["JSON_SORT_KEYS"] = False

api_key = os.environ.get("GROQ_API_KEY")
client = Groq(api_key=api_key) if api_key else None

WHISPER_MODEL = "whisper-large-v3"
DEFAULT_TRANSLATE_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "llama-3.1-8b-instant",
]
SUPPORTED_LANGUAGES = [
    "auto",
    "es",
    "en",
    "sw",
    "pt",
    "fr",
    "de",
    "it",
    "ko",
    "ja",
    "zh",
    "ar",
    "hi",
]


def format_time(seconds):
    total_centi = max(0, int(round(float(seconds) * 100)))
    minutes, remainder = divmod(total_centi, 6000)
    secs = remainder / 100
    return f"[{minutes:02d}:{secs:05.2f}]"


def get(seg, key, default=""):
    if isinstance(seg, dict):
        return seg.get(key, default)
    return getattr(seg, key, default)


def extract_json_object(text):
    if not text:
        return None
    cleaned = text.strip().replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return cleaned[start : end + 1]


def try_translation_batch(batch_lines):
    if not batch_lines:
        return [], None

    env_model = os.environ.get("TRANSLATE_MODEL")
    models = []
    if env_model:
        models.append(env_model)
    for model in DEFAULT_TRANSLATE_MODELS:
        if model not in models:
            models.append(model)

    last_reason = None
    for model in models:
        try:
            if client is None:
                raise RuntimeError("GROQ_API_KEY is not set")

            payload = json.dumps(batch_lines, ensure_ascii=False)
            response = client.chat.completions.create(
                model=model,
                temperature=0.2,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Translate song lyrics into natural, singable English. "
                            "Return JSON only: {'translations': ['line 1', 'line 2', ...]} with exactly one string per input line in the same order."
                        ),
                    },
                    {"role": "user", "content": payload},
                ],
            )
            content = getattr(response.choices[0].message, "content", "") or ""
            json_text = extract_json_object(content)
            if json_text is None:
                raise ValueError("No JSON object found in model reply")

            data = json.loads(json_text)
            translations = data.get("translations", [])
            if not isinstance(translations, list) or len(translations) != len(batch_lines):
                raise ValueError(
                    f"Expected {len(batch_lines)} translations but got {len(translations) if isinstance(translations, list) else 'non-list'}"
                )

            return [str(item) for item in translations], None
        except Exception as exc:
            last_reason = str(exc)
            app.logger.warning("Translation attempt failed for model %s: %s", model, exc)

    return ["(translation failed)"] * len(batch_lines), last_reason or "translation failed"


def translate_lines(lines):
    if not lines:
        return [], None

    translated = []
    warnings = []

    for i in range(0, len(lines), 40):
        batch = lines[i : i + 40]
        translations, failure_reason = try_translation_batch(batch)
        translated.extend(translations)
        if failure_reason:
            warnings.append(failure_reason)

    return translated, "; ".join(warnings) if warnings else None


@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")


@app.route("/sw.js")
def service_worker():
    response = send_from_directory("static", "sw.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/transcribe", methods=["POST"])
def transcribe():
    if not os.environ.get("GROQ_API_KEY"):
        return jsonify({"error": "GROQ_API_KEY is not set on the server"}), 500

    if "audio" not in request.files:
        return jsonify({"error": "No audio file uploaded"}), 400

    language_choice = (request.form.get("language") or "auto").strip().lower()
    if language_choice not in SUPPORTED_LANGUAGES:
        language_choice = "auto"

    file = request.files["audio"]
    audio_bytes = file.read()
    filename = file.filename or "song.mp3"

    try:
        request_kwargs = {
            "file": (filename, audio_bytes),
            "model": WHISPER_MODEL,
            "response_format": "verbose_json",
            "timestamp_granularities": ["segment"],
        }
        if language_choice != "auto":
            request_kwargs["language"] = language_choice

        result = client.audio.transcriptions.create(**request_kwargs)
        segments = result.segments if hasattr(result, "segments") else getattr(result, "segments", [])
        detected_language = getattr(result, "language", None)
        if isinstance(result, dict):
            detected_language = result.get("language") or detected_language

        if not segments:
            return jsonify({"error": "No speech detected in the uploaded audio."}), 422

        starts = []
        texts = []
        for seg in segments:
            text = str(get(seg, "text", "")).strip()
            if text:
                starts.append(float(get(seg, "start", 0.0)))
                texts.append(text)

        if not texts:
            return jsonify({"error": "No speech detected in the uploaded audio."}), 422

        if detected_language == "en" or language_choice == "en":
            lyrics = "\n".join(f"{format_time(start)} _{text}_" for start, text in zip(starts, texts))
            return jsonify({"lyrics": lyrics})

        translated_lines, warning = translate_lines(texts)
        output_lines = []
        for idx, (start_time, text) in enumerate(zip(starts, texts)):
            english_line = translated_lines[idx] if idx < len(translated_lines) else "(translation failed)"
            output_lines.append(f"{format_time(start_time)} _{text}_ → _{english_line}_")

        payload = {"lyrics": "\n".join(output_lines)}
        if warning:
            payload["warning"] = warning
        return jsonify(payload)
    except RateLimitError as exc:
        app.logger.warning("Groq rate limit reached: %s", exc)
        return jsonify({"error": "usage limit reached, try again later"}), 429
    except AuthenticationError as exc:
        app.logger.warning("Groq auth error: %s", exc)
        return jsonify({"error": "Invalid Groq API key."}), 401
    except APIConnectionError as exc:
        app.logger.warning("Groq connection error: %s", exc)
        return jsonify({"error": "Could not reach the Groq API. Please try again later."}), 502
    except RequestEntityTooLarge:
        return jsonify({"error": "File is over 25 MB."}), 413
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)
