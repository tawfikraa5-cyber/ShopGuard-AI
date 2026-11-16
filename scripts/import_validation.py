"""Import completed validation runs without replacing existing reviews."""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app

app.initialize()
directory = app.DATA / "validation"
results = json.loads((directory / "results.json").read_text(encoding="utf-8"))
import sqlite3

with sqlite3.connect(directory / "validation.db") as source, app.connection() as destination:
    source.row_factory = sqlite3.Row
    for result in results:
        if result["status"] != "complete":
            continue
        result.setdefault(
            "analysis_config",
            {"zone": [0.1, 0.1, 0.9, 0.9], "dwell_seconds": 8, "min_confidence": 0.4},
        )
        job_id = Path(result["frame"]).name.split("-preview-")[0]
        filename = result["file"]
        target = app.DATA / "uploads" / filename
        if not target.exists():
            shutil.copyfile(directory / "uploads" / filename, target)
        for row in source.execute("SELECT * FROM events WHERE run_id=?", (job_id,)):
            record = dict(row)
            columns = ",".join(record)
            destination.execute(
                f"INSERT OR IGNORE INTO events ({columns}) VALUES ({','.join('?' for _ in record)})",
                list(record.values()),
            )
            if record["snapshot"]:
                name = Path(record["snapshot"]).name
                shutil.copyfile(directory / "snapshots" / name, app.DATA / "snapshots" / name)
        for image in (directory / "snapshots").glob(job_id + "-preview-*.jpg"):
            shutil.copyfile(image, app.DATA / "snapshots" / image.name)
        (app.DATA / "jobs" / (job_id + ".json")).write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        print("Imported " + filename)
