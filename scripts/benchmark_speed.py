"""Compare full-frame and sampled inference on the same video segment."""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
import cv2
import torch
from vision import load_model

torch.set_num_threads(4)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--video", type=Path, default=Path.home() / "Downloads" / "Shoplifting033_x264.mp4"
)
args = parser.parse_args()
capture = cv2.VideoCapture(str(args.video))
capture.set(cv2.CAP_PROP_POS_MSEC, 12000)
frames = []
for _ in range(180):
    ok, frame = capture.read()
    if not ok:
        break
    frames.append(frame)
capture.release()
if not frames:
    parser.error(f"Cannot read video segment: {args.video}")
model = load_model("yolo11n.pt")
pose = load_model("yolo11n-pose.pt")
model.predict(frames[0], imgsz=640, verbose=False)
pose.predict(frames[0], imgsz=640, verbose=False)
results = {}
for name, stride in [("previous_every_frame", 1), ("balanced_sampled_plus_pose", 5)]:
    for tracker in getattr(model.predictor, "trackers", []):
        tracker.reset()
    started = time.perf_counter()
    count = 0
    pose_count = 0
    for i, frame in enumerate(frames):
        if i % stride:
            continue
        model.track(
            frame,
            persist=True,
            classes=[0],
            conf=0.45 if stride == 1 else 0.1,
            imgsz=640,
            tracker=str(ROOT / "tracker.yaml"),
            verbose=False,
        )
        if stride > 1 and count % 3 == 0:
            pose.predict(frame, imgsz=640, conf=0.25, verbose=False)
            pose_count += 1
        count += 1
    results[name] = {
        "seconds": round(time.perf_counter() - started, 3),
        "detector_frames": count,
        "pose_frames": pose_count,
    }
results["speedup"] = round(
    results["previous_every_frame"]["seconds"] / results["balanced_sampled_plus_pose"]["seconds"], 2
)
results["scope"] = (
    "same 6s source segment, warm CPU inference only; excludes decoding, database and snapshots"
)
(ROOT / "data" / "speed-benchmark.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print(json.dumps(results, indent=2), flush=True)
