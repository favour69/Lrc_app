import json
import os

from flask import Flask, request, jsonify, render_template
from groq import Groq

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # Groq's upload limit is 25 MB

# Set GROQ_API_KEY as an environment variable / secret on your host
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

WHISPER_MODEL = "whisper-large-v3"
TRANSLATE_MODEL = "llama-3.3-70b-versatile"


def format_time(seconds):
    # Converts 65.2 -> [01:05.20]
    m = int(seconds // 60)
    s = seconds % 60
    return f"[{m:02d}:{s:05.2f}]"


def get(seg, key):
    # Segments may come back as dicts or objects depending on SDK version
    return seg[key] if isinstance(seg, dict) else getattr(seg, key)


def translate_lines(lines):
    """Translate a list of Spanish lines to English in a single request."""
    if not lines:
        return []
    numbered = json.dumps(lines, ensure_ascii=False)
    try:
        resp = client.chat.completions.create(
            model=TRANSLATE_MODEL,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You translate Spanish worship song lyrics into natural, "
                        "singable English. You receive a JSON array of lines. "
                        'Reply with JSON only: {"translations": [...]} containing '
                        "exactly one English string per input line, in the same order."
                    ),
                },
                {"role": "user", "content": numbered},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        out = data.get("translations", [])
        if len(out) == len(lines):
            return [str(x) for x in out]
    except Exception:
        pass
    return ["(translation failed)"] * len(lines)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/transcribe", methods=["POST"])
def transcribe():
    if not os.environ.get("GROQ_API_KEY"):
        return jsonify({"error": "GROQ_API_KEY is not set on the server"}), 500
    if "audio" not in request.files:
        return jsonify({"error": "No audio file uploaded"}), 400

    file = request.files["audio"]
    audio_bytes = file.read()
    filename = file.filename or "song.mp3"

    try:
        # 1. Transcribe with segment timestamps (Spanish)
        result = client.audio.transcriptions.create(
            file=(filename, audio_bytes),
            model=WHISPER_MODEL,
            language="es",
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )
        segments = result.segments if hasattr(result, "segments") else result["segments"]

        starts, spanish = [], []
        for seg in segments:
            text = get(seg, "text").strip()
            if text:
                starts.append(get(seg, "start"))
                spanish.append(text)

        # 2. Translate all lines in one call
        english = translate_lines(spanish)

        # 3. Custom format
        lines = [
            f"{format_time(t)} _{es}_ → _{en}_"
            for t, es, en in zip(starts, spanish, english)
        ]
        return jsonify({"lyrics": "\n".join(lines)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
