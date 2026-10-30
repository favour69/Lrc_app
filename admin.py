"""Password-protected admin area: /admin

Disabled completely (404) unless the ADMIN_PASSWORD environment variable is set.
"""

import csv
import hmac
import io
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from flask import Blueprint, Response, abort, g, jsonify, redirect, render_template, request, session, url_for

from metrics import RANGES, hash_ip, store

log = logging.getLogger("admin")

bp = Blueprint("admin", __name__, url_prefix="/admin")

MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60
_failures = {}
_failures_lock = threading.Lock()

_VISITOR_ARG_RE = re.compile(r"^[0-9a-zA-Z\-]{1,64}$")

if os.environ.get("ADMIN_PASSWORD") and len(os.environ["ADMIN_PASSWORD"]) < 12:
    log.warning("ADMIN_PASSWORD is shorter than 12 characters; use a longer one.")


def _password():
    return os.environ.get("ADMIN_PASSWORD", "")


def _authed():
    return session.get("admin") is True


# ---------------------------------------------------------------- brute-force guard
def _recent_failures(key):
    now = time.time()
    with _failures_lock:
        attempts = [t for t in _failures.get(key, []) if now - t < LOCKOUT_SECONDS]
        if attempts:
            _failures[key] = attempts
        else:
            _failures.pop(key, None)
        return attempts


def _locked(key):
    return len(_recent_failures(key)) >= MAX_FAILED_LOGINS


def _note_failure(key):
    with _failures_lock:
        _failures.setdefault(key, []).append(time.time())


def _clear_failures(key):
    with _failures_lock:
        _failures.pop(key, None)


# ---------------------------------------------------------------- request hooks
@bp.before_request
def _gate():
    if not _password():
        abort(404)  # admin area does not exist until a password is configured
    g.nonce = secrets.token_urlsafe(16)


@bp.after_request
def _security_headers(response):
    nonce = getattr(g, "nonce", "")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        "connect-src 'self'; img-src 'self' data:; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    return response


# ---------------------------------------------------------------- pages
@bp.route("/", strict_slashes=False)
def index():
    if not _authed():
        return render_template("admin_login.html", nonce=g.nonce, error=None)
    return render_template("admin.html", nonce=g.nonce)


@bp.post("/login")
def login():
    key = hash_ip(request.remote_addr)
    if _locked(key):
        return render_template("admin_login.html", nonce=g.nonce, error="Too many attempts. Try again in 15 minutes."), 429

    supplied = request.form.get("password", "")
    if hmac.compare_digest(supplied.encode(), _password().encode()):
        _clear_failures(key)
        session.clear()
        session["admin"] = True
        session.permanent = True
        return redirect(url_for("admin.index"))

    _note_failure(key)
    log.warning("Failed admin login from %s", key)
    return render_template("admin_login.html", nonce=g.nonce, error="Incorrect password."), 401


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.index"))


# ---------------------------------------------------------------- data
def _int_arg(name, default, low, high):
    try:
        return max(low, min(high, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


def _range_arg():
    value = request.args.get("range", "7d")
    return value if value in RANGES or value == "all" else "7d"


@bp.get("/api/stats")
def api_stats():
    if not _authed():
        return jsonify({"error": "unauthorized"}), 401

    visitor = request.args.get("visitor")
    if visitor and not _VISITOR_ARG_RE.match(visitor):
        visitor = None

    try:
        data = store.stats(
            range_key=_range_arg(),
            tz_offset_min=_int_arg("tz", 0, -840, 840),
            visitor=visitor,
            limit=_int_arg("limit", 100, 1, 500),
            offset=_int_arg("offset", 0, 0, 1_000_000),
        )
    except Exception:  # noqa: BLE001
        log.exception("Could not read metrics")
        return jsonify({"error": "Could not read the metrics database."}), 503

    data["limits"] = {
        "audio_seconds_hour": _int_arg_env("GROQ_AUDIO_SECONDS_PER_HOUR", 7200),
        "audio_seconds_day": _int_arg_env("GROQ_AUDIO_SECONDS_PER_DAY", 28800),
        "requests_minute": _int_arg_env("GROQ_REQUESTS_PER_MINUTE", 20),
    }
    return jsonify(data)


def _int_arg_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _csv_cell(value):
    """Stop spreadsheet formula injection from visitor-controlled text (file names)."""
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@bp.get("/export.csv")
def export_csv():
    if not _authed():
        return redirect(url_for("admin.index"))

    range_key = _range_arg()
    try:
        rows = store.export_requests(range_key)
    except Exception:  # noqa: BLE001
        log.exception("Could not export metrics")
        abort(503)

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "time_utc", "visitor_id", "ip_hash", "device", "language", "filename", "size_bytes",
        "audio_seconds", "duration_ms", "status", "http_status", "error_kind", "lines", "translation_failed",
    ])
    for row in rows:
        ts = datetime.fromtimestamp(int(row[0]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([ts] + [_csv_cell(cell) for cell in row[1:]])

    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="lrc-requests-{range_key}.csv"'},
    )
