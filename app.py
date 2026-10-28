import json
import os
import re
import threading
import time

from flask import Flask, request, jsonify, render_template, send_from_directory
from groq import APIConnectionError, APIStatusError, AuthenticationError, Groq, RateLimitError

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.config["JSON_SORT_KEYS"] = False

api_key = os.environ.get("GROQ_API_KEY")
client = Groq(api_key=api_key) if api_key else None

WHISPER_MODEL = "whisper-large-v3"

# Preferred chat models for translation, best first. Only models that the
# Groq API key can actually use (per client.models.list()) are tried.
PREFERRED_TRANSLATE_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]
MAX_TRANSLATE_MODELS = 4
MODEL_CACHE_SECONDS = 600
TRANSLATE_BATCH_SIZE = 40
# Model ids containing any of these are not chat models (or not suitable for translation).
EXCLUDED_MODEL_MARKERS = (
    "whisper",
    "guard",
    "tts",
    "embed",
    "orpheus",
    "playai",
    "safeguard",
    "compound",
)

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

# Whisper's verbose_json reports the language as a name ("english"), not a code ("en").
LANGUAGE_NAME_TO_CODE = {
    "english": "en",
    "spanish": "es",
    "swahili": "sw",
    "portuguese": "pt",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "korean": "ko",
    "japanese": "ja",
    "chinese": "zh",
    "arabic": "ar",
    "hindi": "hi",
}

_model_cache = {"timestamp": 0.0, "ids": None}
_model_cache_lock = threading.Lock()


def format_time(seconds):
    total_centi = max(0, int(round(float(seconds) * 100)))
    minutes, remainder = divmod(total_centi, 6000)
    secs = remainder / 100
    return f"[{minutes:02d}:{secs:05.2f}]"


def get(seg, key, default=""):
    if isinstance(seg, dict):
        return seg.get(key, default)
    return getattr(seg, key, default)


def result_field(result, key, default=None):
    """Read a field from a Groq response object, whether it is a dict, an attribute or an extra field."""
    if isinstance(result, dict):
        return result.get(key, default)
    value = getattr(result, key, None)
    if value is not None:
        return value
    extra = getattr(result, "model_extra", None) or {}
    return extra.get(key, default)


def normalize_language(value):
    if not value:
        return None
    text = str(value).strip().lower()
    if text in LANGUAGE_NAME_TO_CODE:
        return LANGUAGE_NAME_TO_CODE[text]
    return text


def same_text(a, b):
    """True when two lyric lines match ignoring case, punctuation and spacing."""
    def squash(value):
        return re.sub(r"[\W_]+", " ", str(value).lower()).strip()

    return squash(a) == squash(b)


def short_error(exc, limit=220):
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def extract_json_object(text):
    if not text:
        return None
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    cleaned = cleaned.strip().replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return cleaned[start : end + 1]


def available_chat_models():
    """Chat model ids this API key can use, cached for MODEL_CACHE_SECONDS. None if the lookup fails."""
    if client is None:
        return None

    with _model_cache_lock:
        cached = _model_cache["ids"]
        if cached is not None and time.time() - _model_cache["timestamp"] < MODEL_CACHE_SECONDS:
            return cached

    try:
        listing = client.models.list()
        ids = [getattr(item, "id", None) for item in getattr(listing, "data", [])]
    except Exception as exc:
        app.logger.warning("Could not list Groq models: %s", exc)
        return None

    chat_ids = [
        model_id
        for model_id in ids
        if model_id and not any(marker in model_id.lower() for marker in EXCLUDED_MODEL_MARKERS)
    ]

    with _model_cache_lock:
        _model_cache["ids"] = chat_ids
        _model_cache["timestamp"] = time.time()
    return chat_ids


def pick_translate_models():
    """Up to MAX_TRANSLATE_MODELS model ids to try, in order."""
    ordered = []

    def add(model_id):
        if model_id and model_id not in ordered:
            ordered.append(model_id)

    add((os.environ.get("TRANSLATE_MODEL") or "").strip())

    available = available_chat_models()
    if available:
        for model_id in PREFERRED_TRANSLATE_MODELS:
            if model_id in available:
                add(model_id)
        for model_id in sorted(available):
            add(model_id)
    else:
        for model_id in PREFERRED_TRANSLATE_MODELS:
            add(model_id)

    return ordered[:MAX_TRANSLATE_MODELS]


def translate_batch_with_model(model, batch_lines):
    payload = json.dumps(batch_lines, ensure_ascii=False)
    kwargs = {}
    if model.startswith("openai/gpt-oss"):
        kwargs["extra_body"] = {"reasoning_effort": "low"}

    response = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "Translate song lyrics into natural, singable English. "
                    "The user message is a JSON array of lyric lines. "
                    'Reply with JSON only, in the form {"translations": ["line 1", "line 2"]}, '
                    "with exactly one string per input line, in the same order. "
                    "Do not merge or split lines. If a line is already English, return it unchanged."
                ),
            },
            {"role": "user", "content": payload},
        ],
        **kwargs,
    )
    content = getattr(response.choices[0].message, "content", "") or ""
    json_text = extract_json_object(content)
    if json_text is None:
        raise ValueError("No JSON object found in model reply")

    data = json.loads(json_text)
    translations = data.get("translations", []) if isinstance(data, dict) else []
    if not isinstance(translations, list) or len(translations) != len(batch_lines):
        got = len(translations) if isinstance(translations, list) else "non-list"
        raise ValueError(f"Expected {len(batch_lines)} translations but got {got}")

    return [str(item).strip() for item in translations]


def translate_lines(lines):
    """Translate lines to English.

    Returns (translations, warning). A translation is None for lines that could
    not be translated; the warning lists every model that failed and why.
    """
    if not lines:
        return [], None

    if client is None:
        return [None] * len(lines), "Translation unavailable: GROQ_API_KEY is not set"

    models = pick_translate_models()
    errors = {}
    translated = []
    failed_lines = 0

    for start in range(0, len(lines), TRANSLATE_BATCH_SIZE):
        batch = lines[start : start + TRANSLATE_BATCH_SIZE]
        result = None

        for model in list(models):
            try:
                result = translate_batch_with_model(model, batch)
            except Exception as exc:
                errors.setdefault(model, short_error(exc))
                app.logger.warning("Translation attempt failed for model %s: %s", model, exc)
                continue

            # Try the model that worked first for the remaining batches.
            models.remove(model)
            models.insert(0, model)
            break

        if result is None:
            failed_lines += len(batch)
            translated.extend([None] * len(batch))
        else:
            translated.extend(result)

    warning = None
    if failed_lines:
        details = "; ".join(f"{model}: {reason}" for model, reason in errors.items())
        warning = f"Translation failed for {failed_lines} of {len(lines)} lines."
        if details:
            warning += f" Models tried: {details}"
    return translated, warning


@app.errorhandler(413)
def file_too_large(_error):
    return jsonify({"error": "File is over 25 MB."}), 413


@app.route("/manifest.json")
def manifest():
    response = send_from_directory("static", "manifest.json", mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "no-cache"
    return response


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
    if client is None:
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
            "temperature": 0.0,
        }
        if language_choice != "auto":
            request_kwargs["language"] = language_choice

        result = client.audio.transcriptions.create(**request_kwargs)
        segments = result_field(result, "segments", []) or []
        detected_language = normalize_language(result_field(result, "language"))

        starts = []
        texts = []
        for seg in segments:
            text = str(get(seg, "text", "")).strip()
            if text:
                starts.append(float(get(seg, "start", 0.0)))
                texts.append(text)

        if not texts:
            return jsonify({"error": "No speech detected in the uploaded audio."}), 422

        app.logger.info("Transcribed %d lines (language choice=%s, detected=%s)", len(texts), language_choice, detected_language)

        # Only skip translation when the user said the song is English. Whisper's auto-detected
        # language comes from the opening seconds, so it is wrong for songs that start with a
        # stray "Thank you." or switch language; lines that are already English are shown once.
        if language_choice == "en":
            lyrics = "\n".join(f"{format_time(start)} _{text}_" for start, text in zip(starts, texts))
            return jsonify({"lyrics": lyrics})

        translated_lines, warning = translate_lines(texts)
        output_lines = []
        for idx, (start_time, text) in enumerate(zip(starts, texts)):
            english_line = translated_lines[idx] if idx < len(translated_lines) else None
            if english_line and not same_text(english_line, text):
                output_lines.append(f"{format_time(start_time)} _{text}_ → _{english_line}_")
            else:
                output_lines.append(f"{format_time(start_time)} _{text}_")

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
    except APIStatusError as exc:
        app.logger.warning("Groq API error %s: %s", exc.status_code, exc)
        if exc.status_code == 413:
            return jsonify({"error": "File is over 25 MB."}), 413
        return jsonify({"error": f"Groq could not process this file: {short_error(exc)}"}), 502
    except Exception as exc:
        app.logger.exception("Unexpected error while transcribing")
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)
