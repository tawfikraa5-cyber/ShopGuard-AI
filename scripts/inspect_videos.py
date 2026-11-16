"""Create timestamped contact sheets from the sample videos."""

import argparse
import json
from pathlib import Path
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--directory", type=Path, default=Path.home() / "Downloads")
args = parser.parse_args()
OUTPUT = ROOT / "data" / "inspection"
OUTPUT.mkdir(parents=True, exist_ok=True)
names = ["044", "050", "051", "005", "033", "048", "001"]
metadata = []
for number in names:
    source = args.directory / f"Shoplifting{number}_x264.mp4"
    capture = cv2.VideoCapture(str(source))
    fps, count = capture.get(5), int(capture.get(7))
    if not capture.isOpened() or fps <= 0:
        capture.release()
        parser.error(f"Cannot read video: {source}")
    sheet = np.full((5 * 265, 4 * 320, 3), 18, dtype=np.uint8)
    timestamps = np.linspace(0, max(0, count - 1), 20).astype(int)
    for i, index in enumerate(timestamps):
        capture.set(1, int(index))
        ok, frame = capture.read()
        if not ok:
            continue
        frame = cv2.resize(frame, (320, 240))
        y, x = (i // 4) * 265, (i % 4) * 320
        sheet[y : y + 240, x : x + 320] = frame
        cv2.putText(
            sheet,
            f"{index / fps:.1f}s",
            (x + 8, y + 257),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (220, 235, 230),
            1,
        )
    capture.release()
    cv2.imwrite(str(OUTPUT / f"{number}.jpg"), sheet)
    metadata.append(
        {
            "file": source.name,
            "fps": fps,
            "frames": count,
            "seconds": count / fps,
            "sheet": str(OUTPUT / f"{number}.jpg"),
        }
    )
(OUTPUT / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
print(json.dumps(metadata, indent=2))
