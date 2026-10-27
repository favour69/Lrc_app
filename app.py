import os
import tempfile

import whisper
from flask import Flask, request, jsonify, render_template
from googletrans import Translator

app = Flask(__name__)

# Load Whisper once - "small" for fast tests, "large-v3" for best quality
print("Loading Whisper...")
model = whisper.load_model("small")  # change to "large-v3" for production
translator = Translator()


def format_time(seconds):
    # Converts 65.2 -> [01:05.20]
    m = int(seconds // 60)
    s = seconds % 60
    return f"[{m:02d}:{s:05.2f}]"


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/transcribe", methods=["POST"])
def transcribe():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file uploaded"}), 400

    file = request.files["audio"]
    suffix = os.path.splitext(file.filename or "")[1] or ".mp3"

    # Save upload to a safe temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        filepath = tmp.name
        file.save(tmp)

    try:
        # 1. Transcribe with timestamps (Spanish)
        result = model.transcribe(filepath, language="es", word_timestamps=False)

        output_lines = []
        for segment in result["segments"]:
            start = segment["start"]
            spanish_text = segment["text"].strip()
            if not spanish_text:
                continue

            # 2. Translate to English
            try:
                english = translator.translate(spanish_text, src="es", dest="en").text
            except Exception:
                english = "(translation failed)"

            # 3. Custom format
            output_lines.append(f"{format_time(start)} _{spanish_text}_ → _{english}_")

        return jsonify({"lyrics": "\n".join(output_lines)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)


if __name__ == "__main__":
    app.run(debug=True)
