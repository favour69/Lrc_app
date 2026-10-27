# Lrc_app

A Flask web app that accepts an audio recording, transcribes it with Groq Whisper, optionally translates the lines to English, and returns timestamped output for the user.

## Features
- Audio upload in the browser
- Groq Whisper transcription with segment timestamps
- Optional translation of song lines into English
- Clean, responsive front end for quick use
- Docker-ready and Render-friendly deployment

## Local setup

1. Clone the repository:
   ```bash
   git clone https://github.com/favour69/Lrc_app.git
   cd Lrc_app
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Create a local environment file from the example:
   ```bash
   cp .env.example .env
   ```

5. Fill in your Groq API key in `.env`:
   ```env
   GROQ_API_KEY=your_groq_api_key_here
   PORT=5000
   ```

6. Start the app:
   ```bash
   python app.py
   ```

7. Open the app in your browser:
   ```text
   http://localhost:5000
   ```

## Render deployment

This project is set up to work well on Render.

### Option 1: Deploy using the included Dockerfile
1. Push this repository to GitHub.
2. In Render, click New > Web Service.
3. Connect the repository.
4. Render will detect the Dockerfile automatically.
5. Add this environment variable in Render:
   - `GROQ_API_KEY`
6. Leave the app port as Render-managed (`PORT`), which the container uses automatically.

### Option 2: Deploy as a Python service without Docker
Use the app directly with:
```bash
gunicorn -b 0.0.0.0:$PORT --timeout 180 app:app
```
Also set the environment variable:
```env
GROQ_API_KEY=your_groq_api_key_here
```

## Environment variables
- `GROQ_API_KEY`: required for transcription and translation
- `PORT`: optional for local and hosting environments

## Notes
- The app limits uploads to 25 MB to match Groq's upload constraints.
- The backend returns a clear error if the Groq API key is missing.
