"""Run the sample videos in a separate validation database."""

import argparse
import json
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app

parser = argparse.ArgumentParser()
parser.add_argument("--directory", type=Path, default=Path.home() / "Downloads")
parser.add_argument(
    "--numbers", nargs="+", default=["033", "005", "050", "048", "001", "051", "044"]
)
args = parser.parse_args()
app.DATA = ROOT / "data" / "validation"
app.DB = app.DATA / "validation.db"
app.initialize()
result_path = app.DATA / "results.json"
results = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else []
for number in args.numbers:
    filename = f"Shoplifting{number}_x264.mp4"
    source = Path(args.directory) / filename
    target = app.DATA / "uploads" / filename
    if not target.exists():
        shutil.copyfile(source, target)
    job = "validation-" + number + "-" + str(int(time.time()))
    app.JOBS[job] = {"status": "running", "progress": 0}
    app.ANALYSIS_LOCK.acquire()
    # Keep the region and threshold fixed between validation runs.
    config = {
        **app.DEFAULTS,
        "zone": [0.1, 0.1, 0.9, 0.9],
        "dwell_seconds": 8,
        "analysis_profile": "balanced",
    }
    worker = threading.Thread(target=app.analyze, args=(job, filename, config))
    print(f"START {filename}", flush=True)
    worker.start()
    last = 0
    while worker.is_alive():
        worker.join(1)
        if time.monotonic() - last > 15:
            print(json.dumps({"file": filename, **app.JOBS[job]}, ensure_ascii=True), flush=True)
            last = time.monotonic()
    with app.connection() as db:
        records = [
            dict(r)
            for r in db.execute("SELECT ai,reasons,evidence FROM events WHERE run_id=?", (job,))
        ]
    result = {
        "file": filename,
        **app.JOBS[job],
        "analysis_config": {
            "zone": config["zone"],
            "dwell_seconds": config["dwell_seconds"],
            "min_confidence": min(0.4, config["confidence"]),
        },
        "alerts": sum(r["ai"] for r in records),
        "tracked_roi_records": len(records),
        "hand_observations": sum(json.loads(r["evidence"]).get("hand_cycles", 0) for r in records),
        "accuracy": "not_measured_no_temporal_ground_truth_or_negative_control_set",
    }
    results = [r for r in results if r["file"] != filename] + [result]
    (app.DATA / "results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("DONE " + json.dumps(result, ensure_ascii=True), flush=True)
print("ALL COMPLETE", flush=True)
