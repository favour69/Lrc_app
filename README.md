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

## Admin page

`/admin` shows usage for the site owner: transcriptions, success rate, visitors, page views, audio
minutes, Groq usage against the free-plan limits, languages, errors, devices, a per-visitor list and a
request log (with CSV export). It does not exist (404) until you set a password.

Setup:
1. Create a free Postgres database (for example at neon.tech) and copy its connection string.
2. In Render, open the service > Environment and add:
   - `ADMIN_PASSWORD` - a long, unique password (12+ characters)
   - `DATABASE_URL` - the connection string from step 1
   - `SECRET_KEY` and `ANALYTICS_SALT` - any long random strings (generated automatically if you deploy from `render.yaml`)
3. Redeploy, then open `https://<your-site>/admin`.

Without `DATABASE_URL` the stats are kept in a local file that Render erases on every redeploy, and the
dashboard says so.

What is recorded: for each page view and transcription, the time, an anonymous visitor ID (random cookie),
a scrambled (salted, hashed) IP address, device type, song language, file name and size, audio length,
processing time and whether it worked. The lyrics and the audio are never stored. Records older than
90 days are deleted automatically (`METRICS_RETENTION_DAYS`). Because this tracks visitors, consider
telling them in a short privacy note, and check what your local data-protection law requires.

Admin sign-in is locked for 15 minutes after 5 wrong passwords from one address, the session lasts 12
hours, and the admin pages are never cached and carry a strict Content-Security-Policy.

## Environment variables
- `GROQ_API_KEY`: required for transcription and translation
- `PORT`: optional for local and hosting environments
- `ADMIN_PASSWORD`, `DATABASE_URL`, `SECRET_KEY`, `ANALYTICS_SALT`, `METRICS_RETENTION_DAYS`: see Admin page above

## Notes
- The app limits uploads to 25 MB to match Groq's upload constraints.
- The backend returns a clear error if the Groq API key is missing.
