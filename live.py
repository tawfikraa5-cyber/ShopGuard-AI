"""Local webcam inference. Raw streams are never recorded; alert snapshots only."""

import json
import threading
import time
import traceback
from datetime import datetime, timezone
from vision import EvidenceTracker, SceneGuard, hand_cues, iou, load_model


class LiveSession:
    def __init__(self, app, session_id, config):
        self.app = app
        self.id = session_id
        self.config = dict(config)
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.ready = False
        self.closed = False
        self.started = time.monotonic()
        self.last_activity = self.started
        self.processed = 0
        self.shot = 1
        self.evidence = EvidenceTracker(
            config["dwell_seconds"], min(0.4, config["confidence"]), sample_interval=0.5
        )
        self.scene = SceneGuard()
        self.model = None
        self.pose = None
        self.last_pose = -1

    def start(self):
        threading.Thread(target=self.prepare, daemon=True).start()
        threading.Thread(target=self.watchdog, daemon=True).start()

    def prepare(self):
        with self.lock:
            try:
                import torch

                self.model = load_model("yolo11n.pt")
                self.pose = load_model("yolo11n-pose.pt")
                self.device = 0 if torch.cuda.is_available() else "cpu"
                if self.device == "cpu":
                    torch.set_num_threads(max(1, min(4, torch.get_num_threads())))
                if self.stopped.is_set():
                    self.finish()
                    return
                self.ready = True
                self.app.set_job(self.id, message="الكاميرا خدامة. كنحللو الحركة مباشرة.")
            except Exception:
                traceback.print_exc()
                self.finish("error", "ما قدرناش نوجدو نموذج الكاميرا. تأكد من تبعيات YOLO.")

    def watchdog(self):
        while not self.stopped.wait(3):
            if time.monotonic() - self.last_activity > 25:
                self.stop("توقف اتصال الكاميرا. التنبيهات اللي تسجلو باقين محفوظين.")
                return

    def finish(self, status="stopped", message="الكاميرا توقفات. التنبيهات باقين للمراجعة."):
        if self.closed:
            return
        self.closed = True
        self.ready = False
        self.stopped.set()
        self.app.set_job(
            self.id,
            status=status,
            message=message,
            elapsed_seconds=round(time.monotonic() - self.started, 2),
        )
        with self.app.JOB_LOCK:
            self.app.LIVE_SESSIONS.pop(self.id, None)
        self.app.ANALYSIS_LOCK.release()

    def stop(self, message=None):
        self.stopped.set()
        with self.lock:
            self.finish(message=message or "الكاميرا توقفات. التنبيهات باقين للمراجعة.")

    def frame(self, payload, zone=None, dwell=None):
        self.last_activity = time.monotonic()
        if self.stopped.is_set():
            return {"error": "الكاميرا توقفات."}, 410
        if not self.ready:
            return {"ready": False, "message": "كنوجدو نموذج الكاميرا…"}, 200
        if not self.lock.acquire(blocking=False):
            return {"ready": False, "message": "كنحللو الصورة السابقة…"}, 200
        try:
            if self.stopped.is_set():
                return {"error": "الكاميرا توقفات."}, 410
            import cv2
            import numpy as np

            frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                return {"error": "صورة الكاميرا ما صالحاش."}, 400
            h, w = frame.shape[:2]
            if w > 1280 or h > 1280:
                return {"error": "صورة الكاميرا كبيرة. استعمل معاينة أصغر."}, 400
            changed = (zone is not None and zone != self.config["zone"]) or (
                dwell is not None and dwell != self.config["dwell_seconds"]
            )
            if changed:
                checked = self.app.validate_config(
                    {
                        **self.config,
                        "zone": zone if zone is not None else self.config["zone"],
                        "dwell_seconds": dwell
                        if dwell is not None
                        else self.config["dwell_seconds"],
                    }
                )
                self.config = checked
                self.evidence = EvidenceTracker(
                    checked["dwell_seconds"], min(0.4, checked["confidence"]), sample_interval=0.5
                )
                self.app.set_job(
                    self.id,
                    analysis_config={
                        "zone": checked["zone"],
                        "dwell_seconds": checked["dwell_seconds"],
                    },
                )
            second = time.monotonic() - self.started
            if self.scene.update(frame):
                self.evidence.reset()
                self.shot += 1
                for tracker in getattr(self.model.predictor, "trackers", []):
                    tracker.reset()
            result = self.model.track(
                frame,
                persist=True,
                classes=[0],
                conf=0.1,
                imgsz=512,
                tracker=str(self.app.ROOT / "tracker.yaml"),
                device=self.device,
                verbose=False,
            )[0]
            detections = []
            boxes = result.boxes
            if boxes is not None and boxes.id is not None:
                for box, tid, confidence in zip(
                    boxes.xyxy.cpu().tolist(),
                    boxes.id.int().cpu().tolist(),
                    boxes.conf.cpu().tolist(),
                ):
                    detections.append(
                        {
                            "id": tid,
                            "box": [box[0] / w, box[1] / h, box[2] / w, box[3] / h],
                            "confidence": confidence,
                        }
                    )
            if detections and second - self.last_pose >= 1:
                pose = self.pose.predict(
                    frame, conf=0.25, imgsz=512, device=self.device, verbose=False
                )[0]
                self.last_pose = second
                if pose.keypoints is not None and pose.boxes is not None:
                    points = pose.keypoints.data.cpu().tolist()
                    pboxes = pose.boxes.xyxyn.cpu().tolist()
                    pairs = sorted(
                        [
                            (iou(d["box"], b), di, pi)
                            for di, d in enumerate(detections)
                            for pi, b in enumerate(pboxes)
                        ],
                        reverse=True,
                    )
                    used = set()
                    matched = set()
                    for overlap, di, pi in pairs:
                        if overlap < 0.4:
                            break
                        if pi in used or di in matched:
                            continue
                        used.add(pi)
                        matched.add(di)
                        z = [v * s for v, s in zip(self.config["zone"], (w, h, w, h))]
                        detections[di]["hands"] = hand_cues(
                            points[pi], z, (detections[di]["box"][3] - detections[di]["box"][1]) * h
                        )
            self.processed += 1
            updates = self.evidence.update(detections, second, self.config["zone"])
            alerts = []
            with self.app.connection() as db:
                for event in updates:
                    # Live mode keeps only actual alerts, not baseline frame history.
                    if not event["ai"]:
                        continue
                    snapshot = f"{event['id']}.jpg"
                    image = frame.copy()
                    target = next((d for d in detections if d["id"] == event["track_id"]), None)
                    if target:
                        a, b, c, d = [int(v * s) for v, s in zip(target["box"], (w, h, w, h))]
                        cv2.rectangle(image, (a, b), (c, d), (80, 220, 140), 2)
                        cv2.putText(
                            image,
                            f"#{event['track_id']}",
                            (a, max(14, b - 4)),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.5,
                            (80, 220, 140),
                            1,
                        )
                    cv2.imwrite(str(self.app.DATA / "snapshots" / snapshot), image)
                    facts = {
                        **event["evidence"],
                        "live": True,
                        "source_size": [w, h],
                        "shot": self.shot,
                        "method": "activity-cues-v3",
                        "not_theft_probability": True,
                        "zone": self.config["zone"],
                        "recording": False,
                    }
                    db.execute(
                        """INSERT INTO events
                    (id,mode,run_id,track_id,created_at,video_second,duration,risk,ai,baseline,snapshot,reasons,evidence)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            event["id"],
                            "real",
                            self.id,
                            event["track_id"],
                            datetime.now(timezone.utc).isoformat(),
                            second,
                            event["duration"],
                            event["risk"],
                            1,
                            0,
                            "/media/snapshots/" + snapshot,
                            json.dumps(event["reasons"]),
                            json.dumps(facts),
                        ),
                    )
                    alerts.append(event["id"])
            self.app.set_job(
                self.id,
                people=len(detections),
                second=round(second, 1),
                processed_frames=self.processed,
                message="الكاميرا خدامة. كنحللو الحركة مباشرة.",
            )
            return {
                "ready": True,
                "people": len(detections),
                "boxes": [{k: d[k] for k in ("id", "box", "confidence")} for d in detections],
                "second": round(second, 1),
                "alerts": alerts,
                "frame_ms": round((time.monotonic() - self.last_activity) * 1000),
            }, 200
        except Exception:
            traceback.print_exc()
            self.finish("error", "وقع مشكل فقراءة الكاميرا. تقدر تعاود تشغّلها.")
            return {"error": "وقع مشكل فمعالجة صورة الكاميرا."}, 500
        finally:
            self.lock.release()
