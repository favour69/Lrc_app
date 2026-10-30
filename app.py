import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import timedelta

from flask import Flask, g, jsonify, make_response, render_template, request, send_from_directory
from groq import APIConnectionError, APIStatusError, AuthenticationError, Groq, RateLimitError
from werkzeug.middleware.proxy_fix import ProxyFix

from admin import bp as admin_bp
from metrics import classify_device, hash_ip, store

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.config["JSON_SORT_KEYS"] = False

if os.environ.get("RENDER") or os.environ.get("TRUST_PROXY"):
    # Behind the host's proxy: trust one hop so request.remote_addr is the real visitor address.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_secret = os.environ.get("SECRET_KEY") or os.environ.get("ADMIN_PASSWORD") or ""
app.secret_key = hashlib.sha256(("lrc-session:" + _secret).encode()).digest() if _secret else os.urandom(32)
app.config.update(
    SESSION_COOKIE_NAME="lrc_admin",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    SESSION_COOKIE_PATH="/admin",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
app.register_blueprint(admin_bp)

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
# Segments Whisper itself flags as probably silence (and is unsure about) are dropped;
# these are the usual "Thank you." / "¡Gracias!" hallucinations over quiet intros and outros.
SILENCE_NO_SPEECH_PROB = 0.6
SILENCE_AVG_LOGPROB = -1.0
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


def looks_like_silence(seg):
    """Whisper's own rule for hallucinated text over silence: likely no speech AND low confidence."""
    try:
        no_speech = float(get(seg, "no_speech_prob", 0.0) or 0.0)
        logprob = float(get(seg, "avg_logprob", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    return no_speech > SILENCE_NO_SPEECH_PROB and logprob < SILENCE_AVG_LOGPROB


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


VISITOR_COOKIE = "lrc_vid"
_VISITOR_RE = re.compile(r"^[0-9a-f]{32}$")


def current_visitor():
    """(visitor id, is_new). Anonymous random id kept in a cookie; no personal data."""
    cookie = request.cookies.get(VISITOR_COOKIE, "")
    if _VISITOR_RE.match(cookie):
        return cookie, False
    return uuid.uuid4().hex, True


@app.route("/")
def home():
    visitor, is_new = current_visitor()
    device = classify_device(request.headers.get("User-Agent", ""))
    if device != "bot":
        store.record_visit(visitor, hash_ip(request.remote_addr), device)

    response = make_response(render_template("index.html"))
    if is_new and device != "bot":
        response.set_cookie(
            VISITOR_COOKIE,
            visitor,
            max_age=365 * 24 * 3600,
            httponly=True,
            samesite="Lax",
            secure=bool(os.environ.get("RENDER")),
        )
    return response


def fail(kind, message, status):
    """JSON error that also tells the metrics what kind of failure it was."""
    if hasattr(g, "tx"):
        g.tx["error_kind"] = kind
    return jsonify({"error": message}), status


@app.after_request
def log_transcription(response):
    if request.method != "POST" or request.path != "/transcribe":
        return response

    tx = getattr(g, "tx", {})
    status = response.status_code
    ok = status < 400
    kind = None
    if not ok:
        kind = tx.get("error_kind") or ("file_too_large" if status == 413 else "error")

    ip_hash = hash_ip(request.remote_addr)
    cookie = request.cookies.get(VISITOR_COOKIE, "")
    visitor = cookie if _VISITOR_RE.match(cookie) else "anon-" + ip_hash
    started = tx.get("t0")
    store.record_request(
        visitor_id=visitor,
        ip_hash=ip_hash,
        device=classify_device(request.headers.get("User-Agent", "")),
        language=tx.get("language"),
        filename=tx.get("filename"),
        size_bytes=tx.get("size_bytes"),
        audio_seconds=tx.get("audio_seconds"),
        duration_ms=int((time.time() - started) * 1000) if started else None,
        status="ok" if ok else "error",
        http_status=status,
        error_kind=kind,
        lines=tx.get("lines"),
        translation_failed=tx.get("translation_failed", False),
    )
    return response


@app.route("/transcribe", methods=["POST"])
def transcribe():
    g.tx = {"t0": time.time()}

    if client is None:
        return fail("no_api_key", "GROQ_API_KEY is not set on the server", 500)

    if "audio" not in request.files:
        return fail("no_file", "No audio file uploaded", 400)

    language_choice = (request.form.get("language") or "").strip().lower()
    if language_choice not in SUPPORTED_LANGUAGES:
        return fail("no_language", "Please choose the song language.", 400)
    g.tx["language"] = language_choice

    file = request.files["audio"]
    audio_bytes = file.read()
    filename = file.filename or "song.mp3"
    g.tx["filename"] = filename
    g.tx["size_bytes"] = len(audio_bytes)

    try:
        request_kwargs = {
            "file": (filename, audio_bytes),
            "model": WHISPER_MODEL,
            "response_format": "verbose_json",
            "timestamp_granularities": ["segment"],
            "temperature": 0.0,
            "language": language_choice,
        }

        result = client.audio.transcriptions.create(**request_kwargs)
        segments = result_field(result, "segments", []) or []
        detected_language = normalize_language(result_field(result, "language"))
        try:
            g.tx["audio_seconds"] = float(result_field(result, "duration") or 0) or None
        except (TypeError, ValueError):
            pass

        starts = []
        texts = []
        for seg in segments:
            text = str(get(seg, "text", "")).strip()
            if text and looks_like_silence(seg):
                app.logger.info("Dropped probable silence hallucination at %.1fs: %r", float(get(seg, "start", 0.0)), text)
                continue
            if text:
                starts.append(float(get(seg, "start", 0.0)))
                texts.append(text)

        if not texts:
            return fail("no_speech", "No speech detected in the uploaded audio.", 422)
        g.tx["lines"] = len(texts)

        app.logger.info("Transcribed %d lines (language choice=%s, detected=%s)", len(texts), language_choice, detected_language)

        # Skip translation for English songs; lines that are already English are shown once.
        if language_choice == "en":
            lyrics = "\n".join(f"{format_time(start)} _{text}_" for start, text in zip(starts, texts))
            return jsonify({"lyrics": lyrics})

        translated_lines, warning = translate_lines(texts)
        g.tx["translation_failed"] = bool(warning)
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
        return fail("groq_rate_limit", "usage limit reached, try again later", 429)
    except AuthenticationError as exc:
        app.logger.warning("Groq auth error: %s", exc)
        return fail("groq_auth", "Invalid Groq API key.", 401)
    except APIConnectionError as exc:
        app.logger.warning("Groq connection error: %s", exc)
        return fail("groq_connection", "Could not reach the Groq API. Please try again later.", 502)
    except APIStatusError as exc:
        app.logger.warning("Groq API error %s: %s", exc.status_code, exc)
        if exc.status_code == 413:
            return fail("file_too_large", "File is over 25 MB.", 413)
        return fail("groq_error", f"Groq could not process this file: {short_error(exc)}", 502)
    except Exception as exc:
        app.logger.exception("Unexpected error while transcribing")
        return fail("server_error", str(exc), 500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)
