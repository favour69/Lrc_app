"""Usage metrics for the admin page.

Stores one row per page visit and one per transcription request. Nothing here ever
stores lyrics or audio, and IP addresses are only kept as a salted hash.

Storage is PostgreSQL when DATABASE_URL is set (survives redeploys, e.g. a free Neon or
Supabase database); otherwise a local SQLite file, which is lost when the host resets.
"""

import hashlib
import hmac
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("metrics")

RETENTION_DAYS = int(os.environ.get("METRICS_RETENTION_DAYS", "90") or 90)
SQLITE_PATH = os.environ.get(
    "METRICS_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics.db")
)

RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}

# Requests that actually reached Groq (and so count against its limits), whatever the outcome.
REACHED_GROQ = (
    "(audio_seconds IS NOT NULL OR error_kind IN "
    "('groq_rate_limit', 'groq_auth', 'groq_connection', 'groq_error'))"
)

_BOT_RE = re.compile(r"bot|crawl|spider|slurp|render/|uptime|monitor|headless|curl|wget|python-requests", re.I)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


# ---------------------------------------------------------------- helpers
def _salt():
    explicit = os.environ.get("ANALYTICS_SALT")
    if explicit:
        return explicit.encode()
    secret = os.environ.get("SECRET_KEY") or os.environ.get("ADMIN_PASSWORD") or ""
    return hashlib.sha256(("lrc-ip-salt:" + secret).encode()).digest()


def hash_ip(ip):
    """Salted, truncated hash so visitors can be told apart without storing addresses."""
    if not ip:
        return "unknown"
    return hmac.new(_salt(), str(ip).encode(), hashlib.sha256).hexdigest()[:16]


def classify_device(user_agent):
    ua = user_agent or ""
    if not ua or _BOT_RE.search(ua):
        return "bot"
    if re.search(r"iPad|Tablet", ua):
        return "tablet"
    if re.search(r"Mobi|Android|iPhone", ua):
        return "mobile"
    return "desktop"


def clean_filename(name, limit=200):
    if not name:
        return None
    return _CONTROL_RE.sub("", str(name)).strip()[:limit] or None


def _num(value, default=0):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value):
    return int(_num(value))


# ---------------------------------------------------------------- store
class Store:
    def __init__(self, database_url="", sqlite_path=SQLITE_PATH, retention_days=RETENTION_DAYS, background=True):
        self.url = (database_url or "").strip()
        self.is_postgres = self.url.startswith(("postgres://", "postgresql://"))
        self.sqlite_path = sqlite_path
        self.retention_days = retention_days
        self.background = background
        self._lock = threading.Lock()
        self._ready = False
        self._last_purge = 0.0

    # ---- connections
    @property
    def backend(self):
        return "postgres" if self.is_postgres else "sqlite"

    @contextmanager
    def _conn(self):
        if self.is_postgres:
            import psycopg

            # prepare_threshold=None keeps this working behind connection poolers (e.g. Neon's -pooler endpoints).
            conn = psycopg.connect(self.url, connect_timeout=8, prepare_threshold=None)
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        else:
            with self._lock:
                conn = sqlite3.connect(self.sqlite_path, timeout=10)
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()

    def _run(self, conn, sql, params=()):
        if self.is_postgres:
            sql = sql.replace("?", "%s")
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur

    def _all(self, conn, sql, params=()):
        return self._run(conn, sql, params).fetchall()

    def _one(self, conn, sql, params=()):
        return self._run(conn, sql, params).fetchone()

    # ---- schema
    def init(self):
        pk = "BIGSERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        real = "DOUBLE PRECISION" if self.is_postgres else "REAL"
        statements = [
            f"""CREATE TABLE IF NOT EXISTS visits (
                id {pk}, ts BIGINT NOT NULL, visitor_id TEXT NOT NULL, ip_hash TEXT, device TEXT)""",
            f"""CREATE TABLE IF NOT EXISTS requests (
                id {pk}, ts BIGINT NOT NULL, visitor_id TEXT NOT NULL, ip_hash TEXT, device TEXT,
                language TEXT, filename TEXT, size_bytes BIGINT, audio_seconds {real},
                duration_ms INTEGER, status TEXT NOT NULL, http_status INTEGER, error_kind TEXT,
                lines INTEGER, translation_failed INTEGER DEFAULT 0)""",
            "CREATE INDEX IF NOT EXISTS idx_visits_ts ON visits (ts)",
            "CREATE INDEX IF NOT EXISTS idx_visits_visitor ON visits (visitor_id)",
            "CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests (ts)",
            "CREATE INDEX IF NOT EXISTS idx_requests_visitor ON requests (visitor_id)",
        ]
        with self._conn() as conn:
            for statement in statements:
                self._run(conn, statement)
        self._ready = True

    def _ensure(self):
        if not self._ready:
            self.init()

    # ---- writes (never allowed to break the app)
    def _dispatch(self, fn, *args):
        def work():
            try:
                self._ensure()
                fn(*args)
                self._maybe_purge()
            except Exception as exc:  # noqa: BLE001 - metrics must never take the app down
                log.warning("Could not record metrics: %s", exc)

        if self.background:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()

    def record_visit(self, visitor_id, ip_hash, device):
        self._dispatch(self._insert_visit, int(time.time()), visitor_id, ip_hash, device)

    def _insert_visit(self, ts, visitor_id, ip_hash, device):
        with self._conn() as conn:
            self._run(
                conn,
                "INSERT INTO visits (ts, visitor_id, ip_hash, device) VALUES (?, ?, ?, ?)",
                (ts, visitor_id, ip_hash, device),
            )

    def record_request(self, **f):
        self._dispatch(self._insert_request, int(time.time()), f)

    def _insert_request(self, ts, f):
        with self._conn() as conn:
            self._run(
                conn,
                """INSERT INTO requests (ts, visitor_id, ip_hash, device, language, filename, size_bytes,
                       audio_seconds, duration_ms, status, http_status, error_kind, lines, translation_failed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    f.get("visitor_id") or "unknown",
                    f.get("ip_hash"),
                    f.get("device"),
                    f.get("language"),
                    clean_filename(f.get("filename")),
                    f.get("size_bytes"),
                    f.get("audio_seconds"),
                    f.get("duration_ms"),
                    f.get("status") or "error",
                    f.get("http_status"),
                    f.get("error_kind"),
                    f.get("lines"),
                    1 if f.get("translation_failed") else 0,
                ),
            )

    def _maybe_purge(self):
        now = time.time()
        if now - self._last_purge < 3600 or self.retention_days <= 0:
            return
        self._last_purge = now
        cutoff = int(now - self.retention_days * 86400)
        with self._conn() as conn:
            self._run(conn, "DELETE FROM visits WHERE ts < ?", (cutoff,))
            self._run(conn, "DELETE FROM requests WHERE ts < ?", (cutoff,))

    # ---- reads
    @staticmethod
    def since_for(range_key, now=None):
        now = int(now or time.time())
        return now - RANGES[range_key] if range_key in RANGES else 0

    def stats(self, range_key="7d", tz_offset_min=0, visitor=None, limit=100, offset=0, now=None):
        self._ensure()
        now = int(now or time.time())
        since = self.since_for(range_key, now)
        tz = int(max(-14 * 60, min(14 * 60, tz_offset_min))) * 60
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        with self._conn() as conn:
            r = self._one(
                conn,
                """SELECT COUNT(*),
                          COALESCE(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), 0),
                          COALESCE(SUM(audio_seconds), 0),
                          COALESCE(SUM(size_bytes), 0),
                          AVG(CASE WHEN status = 'ok' THEN duration_ms END),
                          COALESCE(SUM(translation_failed), 0)
                   FROM requests WHERE ts >= ?""",
                (since,),
            )
            total, ok, audio_s, bytes_, avg_ms, tfail = r
            v = self._one(conn, "SELECT COUNT(*) FROM visits WHERE ts >= ?", (since,))
            uniq = self._one(
                conn,
                """SELECT COUNT(DISTINCT visitor_id) FROM (
                       SELECT visitor_id FROM visits WHERE ts >= ?
                       UNION SELECT visitor_id FROM requests WHERE ts >= ?) t""",
                (since, since),
            )

            day_req = self._all(
                conn,
                """SELECT (ts + ?) / 86400 AS d,
                          SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status = 'ok' THEN 0 ELSE 1 END)
                   FROM requests WHERE ts >= ? GROUP BY d ORDER BY d""",
                (tz, since),
            )
            day_vis = self._all(
                conn,
                "SELECT (ts + ?) / 86400 AS d, COUNT(*) FROM visits WHERE ts >= ? GROUP BY d ORDER BY d",
                (tz, since),
            )
            languages = self._all(
                conn,
                """SELECT COALESCE(language, 'unknown'), COUNT(*) FROM requests WHERE ts >= ?
                   GROUP BY 1 ORDER BY 2 DESC""",
                (since,),
            )
            errors = self._all(
                conn,
                """SELECT COALESCE(error_kind, 'unknown'), COUNT(*) FROM requests
                   WHERE ts >= ? AND status <> 'ok' GROUP BY 1 ORDER BY 2 DESC""",
                (since,),
            )
            devices = self._all(
                conn,
                """SELECT COALESCE(device, 'unknown'), COUNT(DISTINCT visitor_id) FROM visits
                   WHERE ts >= ? GROUP BY 1 ORDER BY 2 DESC""",
                (since,),
            )

            hour = self._one(
                conn,
                f"SELECT COALESCE(SUM(audio_seconds), 0), COUNT(*) FROM requests WHERE ts >= ? AND {REACHED_GROQ}",
                (now - 3600,),
            )
            day = self._one(
                conn,
                f"SELECT COALESCE(SUM(audio_seconds), 0), COUNT(*) FROM requests WHERE ts >= ? AND {REACHED_GROQ}",
                (now - 86400,),
            )
            minute = self._one(
                conn, f"SELECT COUNT(*) FROM requests WHERE ts >= ? AND {REACHED_GROQ}", (now - 60,)
            )

            req_visitors = self._all(
                conn,
                """SELECT visitor_id, COUNT(*),
                          SUM(CASE WHEN status = 'ok' THEN 0 ELSE 1 END),
                          COALESCE(SUM(audio_seconds), 0), MIN(ts), MAX(ts), MAX(ip_hash), MAX(device)
                   FROM requests WHERE ts >= ? GROUP BY visitor_id""",
                (since,),
            )
            vis_visitors = self._all(
                conn,
                """SELECT visitor_id, COUNT(*), MIN(ts), MAX(ts), MAX(ip_hash), MAX(device)
                   FROM visits WHERE ts >= ? GROUP BY visitor_id""",
                (since,),
            )

            where, params = "WHERE ts >= ?", [since]
            if visitor:
                where += " AND visitor_id = ?"
                params.append(visitor)
            recent = self._all(
                conn,
                f"""SELECT id, ts, visitor_id, device, language, filename, size_bytes, audio_seconds,
                           duration_ms, status, http_status, error_kind, lines, translation_failed
                    FROM requests {where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?""",
                tuple(params) + (limit + 1, offset),
            )

        # --- merge visitors
        merged = {}
        for vid, n, errs, audio, first, last, ip, dev in req_visitors:
            merged[vid] = {
                "visitor_id": vid, "requests": _int(n), "errors": _int(errs), "audio_seconds": _num(audio),
                "visits": 0, "first_seen": _int(first), "last_seen": _int(last), "ip_hash": ip, "device": dev,
            }
        for vid, n, first, last, ip, dev in vis_visitors:
            m = merged.setdefault(vid, {
                "visitor_id": vid, "requests": 0, "errors": 0, "audio_seconds": 0.0, "visits": 0,
                "first_seen": _int(first), "last_seen": _int(last), "ip_hash": ip, "device": dev,
            })
            m["visits"] = _int(n)
            m["first_seen"] = min(m["first_seen"], _int(first)) if m["first_seen"] else _int(first)
            m["last_seen"] = max(m["last_seen"], _int(last))
            m["ip_hash"] = m["ip_hash"] or ip
            m["device"] = m["device"] or dev
        visitors = sorted(merged.values(), key=lambda x: x["last_seen"], reverse=True)

        # --- daily series with empty days filled in (days are in the viewer's local time)
        first_day = (since + tz) // 86400 if since else None
        known = [int(d) for d, *_ in day_req] + [int(d) for d, _ in day_vis]
        if first_day is None:
            first_day = min(known) if known else (now + tz) // 86400
        last_day = (now + tz) // 86400
        req_by_day = {int(d): (_int(o), _int(e)) for d, o, e in day_req}
        vis_by_day = {int(d): _int(n) for d, n in day_vis}
        days = []
        for d in range(first_day, last_day + 1):
            o, e = req_by_day.get(d, (0, 0))
            days.append({"day": d * 86400 - tz, "ok": o, "error": e, "visits": vis_by_day.get(d, 0)})

        total, ok = _int(total), _int(ok)
        has_more = len(recent) > limit
        return {
            "range": range_key if range_key in RANGES else "all",
            "backend": self.backend,
            "retention_days": self.retention_days,
            "generated_at": now,
            "totals": {
                "requests": total,
                "ok": ok,
                "errors": total - ok,
                "success_rate": (ok / total) if total else None,
                "visitors": _int(uniq[0]),
                "page_views": _int(v[0]),
                "audio_seconds": _num(audio_s),
                "bytes": _int(bytes_),
                "avg_duration_ms": _num(avg_ms, None),
                "translation_failures": _int(tfail),
            },
            "quota": {
                "audio_seconds_hour": _num(hour[0]),
                "requests_hour": _int(hour[1]),
                "audio_seconds_day": _num(day[0]),
                "requests_day": _int(day[1]),
                "requests_minute": _int(minute[0]),
            },
            "days": days,
            "languages": [{"name": n, "count": _int(c)} for n, c in languages],
            "errors": [{"name": n, "count": _int(c)} for n, c in errors],
            "devices": [{"name": n, "count": _int(c)} for n, c in devices],
            "visitors": visitors[:100],
            "requests": [
                {
                    "id": _int(row[0]), "ts": _int(row[1]), "visitor_id": row[2], "device": row[3],
                    "language": row[4], "filename": row[5], "size_bytes": _int(row[6]) if row[6] is not None else None,
                    "audio_seconds": _num(row[7], None), "duration_ms": _int(row[8]) if row[8] is not None else None,
                    "status": row[9], "http_status": row[10], "error_kind": row[11],
                    "lines": row[12], "translation_failed": bool(row[13]),
                }
                for row in recent[:limit]
            ],
            "has_more": has_more,
            "visitor_filter": visitor,
        }

    def export_requests(self, range_key="all"):
        self._ensure()
        since = self.since_for(range_key)
        with self._conn() as conn:
            return self._all(
                conn,
                """SELECT ts, visitor_id, ip_hash, device, language, filename, size_bytes, audio_seconds,
                          duration_ms, status, http_status, error_kind, lines, translation_failed
                   FROM requests WHERE ts >= ? ORDER BY ts DESC, id DESC""",
                (since,),
            )


def _make_store():
    return Store(database_url=os.environ.get("DATABASE_URL", ""))


store = _make_store()
