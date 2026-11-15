"""Measured, explainable activity cues. This is not a trained theft classifier."""

import math
import uuid
from collections import deque

PROFILES = {
    "fast": {"fps": 4, "imgsz": 512, "pose_fps": 1},
    "balanced": {"fps": 6, "imgsz": 640, "pose_fps": 2},
    "detail": {"fps": 10, "imgsz": 960, "pose_fps": 3},
}
MODELS = {}


def load_model(name):
    from pathlib import Path
    from ultralytics import YOLO

    path = Path(__file__).resolve().parent / name
    if name not in MODELS:
        MODELS[name] = YOLO(str(path))
    model = MODELS[name]
    if model.predictor is not None:
        for tracker in getattr(model.predictor, "trackers", []):
            tracker.reset()
    return model


def iou(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1])
    )
    area = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / area if area > 0 else 0


def hand_cues(points, zone, height):
    """Only confident joints; extension into ROI then return near torso is a cue.

    It does not establish product contact, concealment, possession or intent.
    """
    if len(points) < 17 or any(points[i][2] < 0.5 for i in (5, 6, 11, 12)):
        return {}
    torso = (
        (points[5][0] + points[6][0] + points[11][0] + points[12][0]) / 4,
        (points[5][1] + points[6][1] + points[11][1] + points[12][1]) / 4,
    )
    cues = {}
    for wrist in (9, 10):
        x, y, confidence = points[wrist]
        if confidence < 0.5:
            continue
        distance = math.hypot(x - torso[0], y - torso[1]) / max(height, 0.01)
        extended = zone[0] <= x <= zone[2] and zone[1] <= y <= zone[3] and distance > 0.25
        cues[wrist] = "extended" if extended else "torso" if distance < 0.22 else "other"
    return cues


class EvidenceTracker:
    def __init__(self, dwell_seconds, min_confidence=0.35, sample_interval=1 / 6):
        self.threshold = dwell_seconds
        self.min_confidence = min_confidence
        self.tracks = {}
        self.max_gap = max(0.5, float(sample_interval) * 1.8)

    def reset(self):
        self.tracks.clear()

    def update(self, detections, second, zone):
        updates = []
        for detection in detections:
            track_id = detection["id"]
            box = detection["box"]
            inside = zone[0] <= (box[0] + box[2]) / 2 <= zone[2] and zone[1] <= box[3] <= zone[3]
            track = self.tracks.get(track_id)
            if track is None or second - track["last"] > 2:
                track = {
                    "id": uuid.uuid4().hex,
                    "first": second,
                    "last": second,
                    "inside": False,
                    "dwell": 0.0,
                    "max_dwell": 0.0,
                    "total": 0.0,
                    "entries": deque(),
                    "hands": {},
                    "cycles": deque(),
                    "confidence": deque(maxlen=30),
                    "hits": 0,
                    "alerts": {},
                }
                self.tracks[track_id] = track
            gap = second - track["last"]
            contiguous = 0 < gap <= self.max_gap
            track["hits"] += 1
            track["confidence"].append(detection["confidence"])
            if inside:
                if not track["inside"]:
                    track["entries"].append(second)
                    track["dwell"] = 0
                elif contiguous:
                    track["dwell"] += gap
                    track["total"] += gap
                elif gap > 0.8:
                    track["dwell"] = 0
            else:
                track["dwell"] = 0
            track["inside"] = inside
            track["last"] = second
            track["max_dwell"] = max(track["max_dwell"], track["dwell"])
            for wrist, cue in detection.get("hands", {}).items():
                if cue == "extended":
                    track["hands"].setdefault(wrist, second)
                elif cue == "torso" and wrist in track["hands"]:
                    elapsed = second - track["hands"].pop(wrist)
                    if 0.3 <= elapsed <= 4:
                        # Both hands in the same action count once.
                        if not track["cycles"] or second - track["cycles"][-1] > 0.75:
                            track["cycles"].append(second)
            track["hands"] = {k: v for k, v in track["hands"].items() if second - v <= 4}
            while track["entries"] and second - track["entries"][0] > 60:
                track["entries"].popleft()
            while track["cycles"] and second - track["cycles"][0] > 20:
                track["cycles"].popleft()
            reasons = []
            if track["dwell"] >= self.threshold:
                reasons.append("dwell")
            if len(track["entries"]) >= 3 and track["total"] >= 3:
                reasons.append("reentry")
            if len(track["cycles"]) >= 2:
                reasons.append("hand_return")
            mean = sum(track["confidence"]) / len(track["confidence"])
            stable = (
                track["hits"] >= 3
                and second - track["first"] >= 0.5
                and mean >= self.min_confidence
            )
            facts = {
                "confidence": round(mean, 3),
                "observations": track["hits"],
                "entries": len(track["entries"]),
                "hand_cycles": len(track["cycles"]),
                "observed_zone_seconds": round(track["total"], 2),
                "first_second": track["first"],
            }
            # Each distinct signal gets its own immutable evidence and timestamp.
            # A later hand movement must not be hidden by an earlier dwell alert.
            if stable:
                for reason in reasons:
                    if reason in track["alerts"]:
                        continue
                    event = {
                        "id": uuid.uuid4().hex,
                        "track_id": track_id,
                        "duration": track["max_dwell"],
                        "ai": 1,
                        "baseline": 0,
                        "reasons": [reason],
                        "risk": {"dwell": 40, "reentry": 55, "hand_return": 65}[reason],
                        "evidence": {**facts, "trigger_second": second, "signal": reason},
                    }
                    track["alerts"][reason] = event
                    updates.append(event)
            # A single baseline entry record is independent of alert signals.
            if track["entries"]:
                updates.append(
                    {
                        "id": track["id"],
                        "track_id": track_id,
                        "duration": track["max_dwell"],
                        "ai": 0,
                        "baseline": 1,
                        "risk": 20,
                        "reasons": [],
                        "evidence": {**facts, "trigger_second": None},
                    }
                )
        for track_id in list(self.tracks):
            if second - self.tracks[track_id]["last"] > 2:
                del self.tracks[track_id]
        return updates


class SceneGuard:
    """Cuts invalidate every ID and all accumulated behaviour across shots."""

    def __init__(self):
        self.previous = None

    def update(self, frame):
        import cv2

        small = cv2.resize(frame, (64, 48))
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        cut = False
        if self.previous is not None:
            old_hist, old_gray = self.previous
            distance = cv2.compareHist(hist, old_hist, cv2.HISTCMP_BHATTACHARYYA)
            difference = float(cv2.absdiff(gray, old_gray).mean()) / 255
            cut = distance > 0.30 and difference > 0.18
        self.previous = hist, gray
        return cut
