"""Local HTTP server and persistence for ShopGuard AI."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
os.environ.setdefault("YOLO_CONFIG_DIR", str(DATA / "yolo"))
os.environ.setdefault("MPLCONFIGDIR", str(DATA / "matplotlib"))
DB = DATA / "shopguard.db"
JOBS: dict = {}
JOB_LOCK = threading.Lock()
ANALYSIS_LOCK = threading.Lock()
PERSISTED_AT = {}
LIVE_SESSIONS = {}
MAX_UPLOAD = 150 * 1024 * 1024
DEFAULTS = {
    "shop_name": "حانوت البركة",
    "dwell_seconds": 15,
    "confidence": 0.45,
    "zone": [0.52, 0.22, 0.87, 0.85],
    "product_value": 120,
    "demo": True,
    "analysis_profile": "balanced",
}


@contextmanager
def connection():
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize():
    DATA.mkdir(exist_ok=True)
    (DATA / "uploads").mkdir(exist_ok=True)
    (DATA / "snapshots").mkdir(exist_ok=True)
    (DATA / "jobs").mkdir(exist_ok=True)
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          id TEXT PRIMARY KEY, mode TEXT NOT NULL, run_id TEXT, track_id INTEGER,
          created_at TEXT NOT NULL, video_second REAL DEFAULT 0,
          zone TEXT DEFAULT 'المنطقة 01', duration REAL DEFAULT 0,
          risk INTEGER DEFAULT 35, ai INTEGER DEFAULT 0, baseline INTEGER DEFAULT 1,
          status TEXT DEFAULT 'pending', estimated REAL DEFAULT 0,
          actual REAL DEFAULT 0, snapshot TEXT, note TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS review_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
          created_at TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
          undone INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS events_run ON events(mode,run_id,created_at);
        CREATE TABLE IF NOT EXISTS uploads (
          sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        db.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (json.dumps(DEFAULTS),))
        columns = {r["name"] for r in db.execute("PRAGMA table_info(events)")}
        for name, default in [("reasons", "'[]'"), ("evidence", "'{}'")]:
            if name not in columns:
                db.execute(f"ALTER TABLE events ADD COLUMN {name} TEXT DEFAULT {default}")
        if not db.execute("SELECT 1 FROM events WHERE mode='demo' LIMIT 1").fetchone():
            now = datetime.now(timezone.utc)
            samples = [
                (42, 78, "pending", 0, 1),
                (24, 59, "pending", 0, 1),
                (32, 68, "confirmed", 85, 1),
                (19, 46, "false_positive", 0, 1),
                (58, 86, "pending", 0, 1),
                (21, 52, "confirmed", 120, 1),
                (5, 25, "false_positive", 0, 0),
                (7, 29, "false_positive", 0, 0),
            ]
            for i, (duration, risk, status, actual, ai) in enumerate(samples):
                db.execute(
                    """INSERT INTO events
                (id,mode,run_id,track_id,created_at,video_second,zone,duration,risk,ai,status,estimated,actual)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"demo-{i + 1}",
                        "demo",
                        "demo",
                        i + 1,
                        (now - timedelta(minutes=i * 19 + 2)).isoformat(),
                        i * 23,
                        "المنطقة 01" if i % 2 == 0 else "المنطقة 02",
                        duration,
                        risk,
                        ai,
                        status,
                        120,
                        actual,
                    ),
                )
    # A restarted server cannot resume the previous tracker's state.
    for path in (DATA / "jobs").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("status") == "running" and path.stem not in JOBS:
                record.update(
                    status="interrupted",
                    message="التحليل تقطع مع إعادة التشغيل. النتائج الجزئية باقية.",
                )
                atomic_json(path, record)
        except (OSError, ValueError):
            continue


def settings():
    with connection() as db:
        return {
            **DEFAULTS,
            **json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0]),
        }


def validate_config(config):
    config = dict(config)
    if (
        not isinstance(config["shop_name"], str)
        or len(config["shop_name"]) > 80
        or config["analysis_profile"] not in ("fast", "balanced", "detail")
        or not 1 <= float(config["dwell_seconds"]) <= 300
        or not 0.1 <= float(config["confidence"]) <= 0.95
        or not 0 <= float(config["product_value"]) <= 1000000
        or not isinstance(config["demo"], bool)
    ):
        raise ValueError("settings")
    z = config["zone"]
    if (
        not isinstance(z, list)
        or len(z) != 4
        or not all(isinstance(v, (float, int)) and math.isfinite(v) and 0 <= v <= 1 for v in z)
        or z[0] >= z[2]
        or z[1] >= z[3]
    ):
        raise ValueError("zone")
    for key in ("dwell_seconds", "confidence", "product_value"):
        config[key] = float(config[key])
    return config


class DwellTracker:
    """Track continuous presence within an image region."""

    def __init__(self, threshold: float):
        self.threshold = threshold
        self.visits = {}

    def update(self, detections: list, second: float, zone: list):
        updates = []
        present = set()
        for track_id, x, y in detections:
            if not (zone[0] <= x <= zone[2] and zone[1] <= y <= zone[3]):
                continue
            present.add(track_id)
            visit = self.visits.get(track_id)
            if visit is None or second - visit["last"] > 1:
                visit = {"id": uuid.uuid4().hex, "start": second, "last": second}
                self.visits[track_id] = visit
            visit["last"] = second
            duration = max(0, second - visit["start"])
            updates.append(
                {
                    "id": visit["id"],
                    "track_id": track_id,
                    "duration": duration,
                    "ai": int(duration >= self.threshold),
                }
            )
        for track_id in list(self.visits):
            if track_id not in present:
                del self.visits[track_id]
        return updates


def set_job(job_id, **values):
    with JOB_LOCK:
        JOBS[job_id].update(values)
        record = dict(JOBS[job_id])
    now = time.monotonic()
    if record.get("status") != "running" or now - PERSISTED_AT.get(job_id, 0) >= 2:
        directory = DATA / "jobs"
        directory.mkdir(exist_ok=True)
        atomic_json(directory / (job_id + ".json"), record)
        PERSISTED_AT[job_id] = now


def atomic_json(path, record):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def is_cancelled(job_id):
    with JOB_LOCK:
        return bool(JOBS.get(job_id, {}).get("cancel_requested"))


def review_event(event_id, status, amount, note, expected=None):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status,actual,note FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            return None
        before = dict(row)
        if expected is not None and before["status"] != expected:
            raise RuntimeError("review_conflict")
        after = {"status": status, "actual": amount if status == "confirmed" else 0, "note": note}
        db.execute(
            "UPDATE events SET status=?,actual=?,note=? WHERE id=?",
            (after["status"], after["actual"], after["note"], event_id),
        )
        cursor = db.execute(
            "INSERT INTO review_history(event_id,created_at,before_json,after_json) VALUES (?,?,?,?)",
            (
                event_id,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(before),
                json.dumps(after),
            ),
        )
        return cursor.lastrowid


def analyze(job_id, filename, config):
    import sys
    from processing import run_analysis

    run_analysis(sys.modules[__name__], job_id, filename, config)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, obj, status=200):
        payload = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not 0 <= length <= 16384:
            raise ValueError("الطلب كبير بزاف.")
        return json.loads(self.rfile.read(length) or "{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            mode = parse_qs(parsed.query).get("mode", ["demo"])[0]
            run = parse_qs(parsed.query).get("run_id", [None])[0]
            with connection() as db:
                query = "SELECT * FROM events WHERE mode=?"
                args = [mode]
                if run:
                    query += " AND run_id=?"
                    args.append(run)
                events = [
                    dict(row) for row in db.execute(query + " ORDER BY created_at DESC", args)
                ]
            self.send_json({"events": events, "settings": settings(), "mode": mode})
            return
        if parsed.path == "/api/runs":
            records = []
            for path in sorted(
                (DATA / "jobs").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )[:20]:
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    records.append(
                        {
                            "id": path.stem,
                            "source_name": item.get("source_name", "فيديو مسجل"),
                            "status": item.get("status"),
                            "profile": item.get("profile"),
                            "dwell_seconds": item.get("analysis_config", {}).get("dwell_seconds"),
                            "engine_version": item.get("engine_version"),
                            "created_at": item.get("created_at"),
                            "progress": item.get("progress", 0),
                        }
                    )
                except (OSError, ValueError):
                    continue
            self.send_json(records)
            return
        if parsed.path.startswith("/api/jobs/"):
            import re

            job_id = parsed.path.rsplit("/", 1)[-1]
            with JOB_LOCK:
                job = JOBS.get(job_id)
            if job is None and re.fullmatch(r"[a-zA-Z0-9-]{1,100}", job_id):
                try:
                    job = json.loads(
                        (DATA / "jobs" / (job_id + ".json")).read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    pass
            self.send_json(
                dict(job) if job else {"error": "التحليل ما لقيناهش."}, 200 if job else 404
            )
            return
        if parsed.path == "/api/health":
            import importlib.util

            with JOB_LOCK:
                active = next(
                    (
                        {"id": key, **value}
                        for key, value in JOBS.items()
                        if value.get("status") == "running"
                    ),
                    None,
                )
            self.send_json(
                {
                    "ok": True,
                    "version": 4,
                    "active_job": active,
                    "features": {"live_camera": True, "review_undo": True},
                    "video_ready": all(
                        importlib.util.find_spec(x) is not None for x in ("cv2", "ultralytics")
                    ),
                }
            )
            return
        if parsed.path == "/api/export":
            import csv
            import io

            params = parse_qs(parsed.query)
            mode = params.get("mode", ["real"])[0]
            run = params.get("run_id", [None])[0]
            fields = [
                "id",
                "run_id",
                "track_id",
                "created_at",
                "video_second",
                "duration",
                "status",
                "reasons",
                "note",
            ]
            query = "SELECT " + ",".join(fields) + " FROM events WHERE mode=? AND ai=1"
            args = [mode]
            if run:
                query += " AND run_id=?"
                args.append(run)
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(fields)
            with connection() as db:
                for row in db.execute(query + " ORDER BY created_at", args):
                    writer.writerow(
                        [
                            ("'" + str(v))
                            if str(v).startswith(("=", "+", "-", "@", "\t", "\r"))
                            else v
                            for v in row
                        ]
                    )
            payload = ("\ufeff" + output.getvalue()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="shopguard-events.csv"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path.startswith("/media/"):
            pieces = unquote(parsed.path[len("/media/") :]).split("/", 1)
            if len(pieces) != 2 or pieces[0] not in ("uploads", "snapshots"):
                self.send_json({"error": "هاد الملف ما متاحش."}, 404)
                return
            base, relative = DATA / pieces[0], pieces[1]
        else:
            base, relative = ROOT / "static", parsed.path.lstrip("/") or "index.html"
        target = (base / unquote(relative)).resolve()
        if not target.is_relative_to(base.resolve()) or not target.is_file():
            self.send_json({"error": "هاد الملف ما كاينش."}, 404)
            return
        size = target.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range")
        partial = False
        if range_header:
            import re

            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            try:
                if not match or not any(match.groups()):
                    raise ValueError("range")
                a, b = match.groups()
                if not a:
                    count = int(b)
                    if count <= 0:
                        raise ValueError("range")
                    start = max(0, size - count)
                else:
                    start = int(a)
                    end = min(size - 1, int(b)) if b else size - 1
                if start >= size or end < start:
                    raise ValueError("range")
                partial = True
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        self.send_header(
            "Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            with target.open("rb") as file:
                file.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    block = file.read(min(256 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        # All writes are same-origin; reject cross-origin form submissions.
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self.send_json({"error": "هاد الطلب ما مسموحش."}, 403)
            return
        try:
            self.handle_post()
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            self.send_json({"error": "المعلومات ما صالحاش، تأكد من القيم اللي دخلتي."}, 400)

    def handle_post(self):
        path = urlparse(self.path).path
        if path == "/api/live/frame":
            session_id = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            with JOB_LOCK:
                session = LIVE_SESSIONS.get(session_id)
            if session is None:
                self.send_json({"error": "جلسة الكاميرا ما بقاتش خدامة."}, 404)
                return
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 2 * 1024 * 1024:
                raise ValueError("frame size")
            payload = self.rfile.read(size)
            zone = json.loads(self.headers["X-Zone"]) if self.headers.get("X-Zone") else None
            dwell = (
                float(self.headers["X-Dwell-Seconds"])
                if self.headers.get("X-Dwell-Seconds")
                else None
            )
            validate_config(
                {
                    **session.config,
                    "zone": zone if zone is not None else session.config["zone"],
                    "dwell_seconds": dwell
                    if dwell is not None
                    else session.config["dwell_seconds"],
                }
            )
            result, status = session.frame(payload, zone, dwell)
            self.send_json(result, status)
            return
        if path == "/api/upload":
            extension = Path(unquote(self.headers.get("X-Filename", "video.mp4"))).suffix.lower()
            length = int(self.headers.get("Content-Length", 0))
            if extension not in (".mp4", ".mov", ".avi", ".webm") or not 0 < length <= MAX_UPLOAD:
                self.send_json({"error": "اختار فيديو MP4، MOV، AVI أو WebM أصغر من 150 MB."}, 400)
                return
            name = uuid.uuid4().hex + extension
            remaining = length
            fingerprint = hashlib.sha256()
            target = DATA / "uploads" / name
            with target.open("wb") as file:
                while remaining:
                    block = self.rfile.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    file.write(block)
                    fingerprint.update(block)
                    remaining -= len(block)
            if remaining:
                target.unlink(missing_ok=True)
                raise ValueError("Incomplete upload")
            digest = fingerprint.hexdigest()
            with connection() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT filename FROM uploads WHERE sha256=?", (digest,)
                ).fetchone()
                if existing and (DATA / "uploads" / existing["filename"]).is_file():
                    target.unlink()
                    self.send_json({"filename": existing["filename"], "reused": True})
                    return
                db.execute(
                    "INSERT OR REPLACE INTO uploads VALUES (?,?,?)",
                    (digest, name, datetime.now(timezone.utc).isoformat()),
                )
            self.send_json({"filename": name, "reused": False})
            return
        body = self.read_json()
        if path == "/api/live/start":
            from live import LiveSession

            config = settings()
            overrides = body.get("analysis_options", {})
            if not isinstance(overrides, dict):
                raise ValueError("options")
            config.update(
                {
                    key: value
                    for key, value in overrides.items()
                    if key in ("zone", "dwell_seconds", "confidence")
                }
            )
            config = validate_config(config)
            if not ANALYSIS_LOCK.acquire(blocking=False):
                self.send_json({"error": "كاين تحليل آخر خدام. وقفُه الأول باش تشغّل الكاميرا."}, 409)
                return
            session_id = uuid.uuid4().hex
            session = LiveSession(__import__("sys").modules[__name__], session_id, config)
            with JOB_LOCK:
                JOBS[session_id] = {
                    "status": "running",
                    "live": True,
                    "progress": 0,
                    "people": 0,
                    "source_name": "الكاميرا المباشرة",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "engine_version": "activity-cues-v3",
                    "message": "كنوجدو الكاميرا…",
                    "analysis_config": {
                        "zone": config["zone"],
                        "dwell_seconds": config["dwell_seconds"],
                    },
                }
                LIVE_SESSIONS[session_id] = session
            set_job(session_id)
            session.start()
            self.send_json({"id": session_id}, 202)
        elif path == "/api/live/stop":
            with JOB_LOCK:
                session = LIVE_SESSIONS.get(body.get("id"))
            if session:
                session.stop()
            self.send_json({"ok": True})
        elif path == "/api/settings":
            config = settings()
            for key in DEFAULTS:
                if key in body:
                    config[key] = body[key]
            config = validate_config(config)
            with connection() as db:
                db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(config),))
            self.send_json(config)
        elif path == "/api/review":
            status = body["status"]
            amount = float(body.get("actual", 0))
            if (
                status not in ("pending", "confirmed", "false_positive")
                or not math.isfinite(amount)
                or not 0 <= amount <= 1000000
            ):
                raise ValueError("review")
            try:
                token = review_event(
                    body["id"],
                    status,
                    amount,
                    str(body.get("note", ""))[:1000],
                    body.get("expected_status"),
                )
            except RuntimeError:
                self.send_json(
                    {"error": "هاد الحدث تراجع من قبل فنافذة أخرى. تحدّثات المعطيات، عاود شوفو."},
                    409,
                )
                return
            self.send_json(
                {"ok": True, "review_id": token} if token else {"error": "الحدث ما لقيناهش."},
                200 if token else 404,
            )
        elif path == "/api/review/undo":
            token = int(body["review_id"])
            with connection() as db:
                db.execute("BEGIN IMMEDIATE")
                history = db.execute("SELECT * FROM review_history WHERE id=?", (token,)).fetchone()
                if history is None:
                    self.send_json({"error": "المراجعة ما لقيناهاش."}, 404)
                    return
                latest = db.execute(
                    "SELECT id FROM review_history WHERE event_id=? AND undone=0 ORDER BY id DESC LIMIT 1",
                    (history["event_id"],),
                ).fetchone()
                current = db.execute(
                    "SELECT status,actual,note FROM events WHERE id=?", (history["event_id"],)
                ).fetchone()
                if (
                    history["undone"]
                    or latest is None
                    or latest["id"] != token
                    or current is None
                    or dict(current) != json.loads(history["after_json"])
                ):
                    self.send_json(
                        {
                            "error": "القرار تبدّل من بعد. ما قدرناش نتراجعو عليه باش ما نمسحوش مراجعة أخرى."
                        },
                        409,
                    )
                    return
                before = json.loads(history["before_json"])
                db.execute(
                    "UPDATE events SET status=?,actual=?,note=? WHERE id=?",
                    (before["status"], before["actual"], before["note"], history["event_id"]),
                )
                db.execute("UPDATE review_history SET undone=1 WHERE id=?", (token,))
            self.send_json({"ok": True, "event_id": history["event_id"]})
        elif path == "/api/cancel":
            session = None
            with JOB_LOCK:
                job = JOBS.get(body["id"])
                if job is None:
                    self.send_json({"error": "التحليل ما لقيناهش."}, 404)
                    return
                if job.get("status") == "running":
                    job["cancel_requested"] = True
                if job.get("live"):
                    session = LIVE_SESSIONS.get(body["id"])
                status = job.get("status")
            if session:
                session.stop()
            self.send_json({"ok": True, "status": status})
        elif path == "/api/analyze":
            filename = body["filename"]
            if Path(filename).name != filename or not (DATA / "uploads" / filename).is_file():
                raise ValueError("filename")
            config = settings()
            overrides = body.get("analysis_options", {})
            if not isinstance(overrides, dict):
                raise ValueError("analysis_options")
            config.update(
                {
                    key: value
                    for key, value in overrides.items()
                    if key in ("zone", "dwell_seconds", "confidence", "analysis_profile")
                }
            )
            config = validate_config(config)
            config["source_name"] = str(body.get("source_name", filename))[:150]
            if not ANALYSIS_LOCK.acquire(blocking=False):
                self.send_json({"error": "كاين تحليل خدام دابا. تسنّاه يسالي الأول."}, 409)
                return
            job_id = uuid.uuid4().hex
            with JOB_LOCK:
                JOBS[job_id] = {
                    "status": "running",
                    "progress": 0,
                    "people": 0,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "source_name": str(body.get("source_name", filename))[:150],
                    "source_url": "/media/uploads/" + filename,
                    "analysis_config": {
                        "zone": config["zone"],
                        "dwell_seconds": config["dwell_seconds"],
                    },
                    "message": "التحليل غادي يبدا…",
                }
            set_job(job_id)
            threading.Thread(target=analyze, args=(job_id, filename, config), daemon=True).start()
            self.send_json({"id": job_id}, 202)
        else:
            self.send_json({"error": "هاد العملية ما كايناش."}, 404)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    initialize()
    print(f"ShopGuard AI: http://127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
