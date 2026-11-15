"""Sampled YOLO + pose evidence, original video timing, bounded disk work."""

import json
import math
import time
import traceback
from pathlib import Path
from datetime import datetime, timezone
from vision import PROFILES, EvidenceTracker, SceneGuard, hand_cues, iou, load_model


def run_analysis(app, job_id, filename, config):
    capture = None
    started = time.perf_counter()
    try:
        import cv2
        import torch

        profile = PROFILES[config.get("analysis_profile", "balanced")]
        if app.is_cancelled(job_id):
            app.set_job(
                job_id, status="cancelled", message="توقف التحليل. النتائج اللي واجدة باقية محفوظة."
            )
            return
        app.set_job(job_id, message="كنوجدو الكشف وتتبع الحركة…", engine_version="activity-cues-v3")
        model = load_model("yolo11n.pt")
        pose = load_model("yolo11n-pose.pt")
        device = 0 if torch.cuda.is_available() else "cpu"
        # Keep CPU responsive while the local web server handles review requests.
        if device == "cpu":
            torch.set_num_threads(max(1, min(4, torch.get_num_threads())))
        capture = cv2.VideoCapture(str(app.DATA / "uploads" / filename))
        if not capture.isOpened():
            raise RuntimeError("ما قدرناش نقراو الفيديو. جرّب MP4 بترميز H.264.")
        fps = capture.get(cv2.CAP_PROP_FPS)
        total = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        if not math.isfinite(fps) or fps <= 0:
            raise RuntimeError("توقيت الفيديو ما صالحش باش نحسبو مدة الحركة.")
        stride = max(1, round(fps / profile["fps"]))
        pose_stride = max(1, round(fps / stride / profile["pose_fps"]))
        evidence = EvidenceTracker(
            config["dwell_seconds"], min(0.4, config["confidence"]), sample_interval=stride / fps
        )
        scene = SceneGuard()
        shot = 1
        cuts = []
        index = 0
        processed = 0
        pose_frames = 0
        first_seen = {}
        pending = {}
        saved_alerts = set()
        last_publish = -1.0
        last_frame = None
        width = int(capture.get(3))
        height = int(capture.get(4))
        warnings = []
        stopped = False
        if min(width, height) < 360:
            warnings.append("الدقة الأصلية ضعيفة؛ تفاصيل اليدين والسلعة تقدر ما تبانش.")
        app.set_job(
            job_id,
            source_name=config.get("source_name", filename),
            source_size=[width, height],
            source_fps=fps,
            sample_fps=round(fps / stride, 2),
            profile=config.get("analysis_profile", "balanced"),
            device=str(device),
            warnings=warnings,
            analysis_config={
                "zone": config["zone"],
                "dwell_seconds": config["dwell_seconds"],
                "min_confidence": min(0.4, config["confidence"]),
            },
        )

        def annotate(frame, detections):
            # Larger rendering improves label legibility, not source detail.
            scale = max(1, min(2, 960 / frame.shape[1]))
            image = cv2.resize(frame, None, fx=scale, fy=scale) if scale > 1 else frame.copy()
            h, w = image.shape[:2]
            for d in detections:
                a, b, c, f = [int(v * s) for v, s in zip(d["box"], (w, h, w, h))]
                cv2.rectangle(image, (a, b), (c, f), (105, 210, 90), 1)
                cv2.putText(
                    image,
                    f"#{d['id']} {d['confidence']:.0%}",
                    (a, max(12, b - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.38,
                    (140, 245, 155),
                    1,
                    cv2.LINE_AA,
                )
            z = config["zone"]
            cv2.rectangle(
                image,
                (int(z[0] * w), int(z[1] * h)),
                (int(z[2] * w), int(z[3] * h)),
                (50, 190, 230),
                1,
            )
            return image

        def flush(db):
            for v in pending.values():
                db.execute(
                    """INSERT INTO events
                (id,mode,run_id,track_id,created_at,video_second,duration,risk,ai,baseline,estimated,snapshot,reasons,evidence)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                duration=excluded.duration,risk=excluded.risk,ai=excluded.ai,
                snapshot=COALESCE(excluded.snapshot,events.snapshot),reasons=excluded.reasons,evidence=excluded.evidence""",
                    (
                        v["id"],
                        "real",
                        job_id,
                        v["track_id"],
                        first_seen[v["id"]][0],
                        first_seen[v["id"]][1],
                        v["duration"],
                        v["risk"],
                        v["ai"],
                        v.get("baseline", 1),
                        config["product_value"],
                        v.get("snapshot"),
                        json.dumps(v["reasons"]),
                        json.dumps(v["evidence"]),
                    ),
                )
            db.commit()
            pending.clear()

        with app.connection() as db:
            while True:
                if app.is_cancelled(job_id):
                    stopped = True
                    break
                if not capture.grab():
                    break
                current = index
                index += 1
                if current % stride:
                    continue
                ok, frame = capture.retrieve()
                if not ok:
                    continue
                second = current / fps
                if scene.update(frame):
                    flush(db)
                    evidence.reset()
                    shot += 1
                    cuts.append(round(second, 2))
                    if len(cuts) == 1:
                        warnings.append(
                            "تبدلات زاوية الكاميرا؛ تأكد أن المنطقة المحددة مازال كتوافق المكان اللي بغيتي."
                        )
                        app.set_job(job_id, warnings=list(warnings))
                    for tracker in getattr(model.predictor, "trackers", []):
                        tracker.reset()
                # Pass low-score candidates to ByteTrack for association; new IDs
                # require .35 confidence and evidence needs repeated observations.
                result = model.track(
                    frame,
                    persist=True,
                    classes=[0],
                    tracker=str(app.ROOT / "tracker.yaml"),
                    conf=0.1,
                    imgsz=profile["imgsz"],
                    device=device,
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
                                "box": [
                                    box[0] / width,
                                    box[1] / height,
                                    box[2] / width,
                                    box[3] / height,
                                ],
                                "confidence": confidence,
                            }
                        )
                if detections and processed % pose_stride == 0:
                    pr = pose.predict(
                        frame, conf=0.25, imgsz=profile["imgsz"], device=device, verbose=False
                    )[0]
                    pose_frames += 1
                    if pr.keypoints is not None and pr.boxes is not None:
                        used = set()
                        pboxes = pr.boxes.xyxyn.cpu().tolist()
                        points = pr.keypoints.data.cpu().tolist()
                        candidates = sorted(
                            [
                                (iou(d["box"], b), di, pi)
                                for di, d in enumerate(detections)
                                for pi, b in enumerate(pboxes)
                            ],
                            reverse=True,
                        )
                        matched = set()
                        for overlap, di, pi in candidates:
                            if overlap < 0.4:
                                break
                            if pi in used or di in matched:
                                continue
                            used.add(pi)
                            matched.add(di)
                            zp = [
                                v * s
                                for v, s in zip(config["zone"], (width, height, width, height))
                            ]
                            detections[di]["hands"] = hand_cues(
                                points[pi],
                                zp,
                                (detections[di]["box"][3] - detections[di]["box"][1]) * height,
                            )
                processed += 1
                updates = evidence.update(detections, second, config["zone"])
                publish = second - last_publish >= 0.5
                annotated = None
                for v in updates:
                    first_seen.setdefault(v["id"], (datetime.now(timezone.utc).isoformat(), second))
                    v["evidence"].update(
                        {
                            "shot": shot,
                            "source": "/media/uploads/" + filename,
                            "sample_fps": round(fps / stride, 2),
                            "source_fps": fps,
                            "source_size": [width, height],
                            "scene_cuts": len(cuts),
                            "method": "activity-cues-v3",
                            "not_theft_probability": True,
                        }
                    )
                    trigger = v["evidence"]["trigger_second"]
                    if trigger is not None:
                        v["evidence"]["clip_start"] = max(0, round(trigger - 5, 2))
                        v["evidence"]["clip_end"] = (
                            round(min(total / fps, trigger + 5), 2) if total > 0 else trigger + 5
                        )
                    if v["ai"] and v["id"] not in saved_alerts:
                        if annotated is None:
                            annotated = annotate(frame, detections)
                        cv2.imwrite(str(app.DATA / "snapshots" / f"{v['id']}.jpg"), annotated)
                        saved_alerts.add(v["id"])
                    if v["id"] in saved_alerts:
                        v["snapshot"] = "/media/snapshots/" + v["id"] + ".jpg"
                    pending[v["id"]] = v
                if publish:
                    flush(db)
                    if annotated is None:
                        annotated = annotate(frame, detections)
                    # Versioned preview avoids replacement races with Windows readers.
                    frame_name = f"{job_id}-preview-{round(second * 2) % 3}.jpg"
                    target = app.DATA / "snapshots" / frame_name
                    temp = target.with_name(target.stem + "-temp.jpg")
                    cv2.imwrite(str(temp), annotated)
                    temp.replace(target)
                    last_frame = "/media/snapshots/" + frame_name
                    elapsed = time.perf_counter() - started
                    app.set_job(
                        job_id,
                        progress=min(99, round(current / total * 100)) if total > 0 else 0,
                        people=len(detections),
                        second=round(second, 1),
                        frame=last_frame,
                        processed_frames=processed,
                        pose_frames=pose_frames,
                        scene_cuts=cuts,
                        elapsed_seconds=round(elapsed, 2),
                        speed=round(second / max(elapsed, 0.001), 2),
                        message="كنحللو البقاء والرجوع وحركة اليد…",
                    )
                    last_publish = second
            flush(db)
        if stopped:
            app.set_job(
                job_id,
                status="cancelled",
                processed_frames=processed,
                pose_frames=pose_frames,
                elapsed_seconds=round(time.perf_counter() - started, 2),
                message="توقف التحليل. الأحداث اللي تلقاو باقين للمراجعة.",
            )
            return
        if processed == 0:
            raise RuntimeError("الفيديو خاوي أو الترميز ما مدعومش.")
        elapsed = time.perf_counter() - started
        app.set_job(
            job_id,
            status="complete",
            progress=100,
            processed_frames=processed,
            pose_frames=pose_frames,
            scene_cuts=cuts,
            elapsed_seconds=round(elapsed, 2),
            speed=round((index / fps) / max(elapsed, 0.001), 2),
            message="التحليل سالا. راجع أسباب التنبيهات والمقاطع.",
        )
    except Exception as exc:
        traceback.print_exc()
        app.set_job(
            job_id,
            status="error",
            message=str(exc)
            if isinstance(exc, RuntimeError)
            else "وقع مشكل فقراءة الفيديو أو النموذج. تأكد من التبعيات وعاود جرّب.",
        )
    finally:
        if capture is not None:
            capture.release()
        app.ANALYSIS_LOCK.release()
