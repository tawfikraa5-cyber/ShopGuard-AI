"""Serve an isolated review fixture on port 8002."""

import json
import shutil
import sys
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app

source = app.DATA / "uploads" / "Shoplifting005_x264.mp4"
app.DATA = ROOT / "data" / "ui-fixture-v3"
app.DB = app.DATA / "test.db"
app.initialize()
if source.exists():
    target = app.DATA / "uploads" / "fixture.mp4"
    if not target.exists():
        shutil.copyfile(source, target)
    facts = {
        "source": "/media/uploads/fixture.mp4",
        "source_fps": 30,
        "clip_start": 33,
        "clip_end": 43,
        "trigger_second": 38,
        "confidence": 0.76,
    }
    with app.connection() as db:
        db.execute(
            "UPDATE events SET reasons=?,evidence=? WHERE id=?",
            (json.dumps(["hand_return"]), json.dumps(facts), "demo-1"),
        )
print("Isolated review QA http://127.0.0.1:8002", flush=True)
ThreadingHTTPServer(("127.0.0.1", 8002), app.Handler).serve_forever()
