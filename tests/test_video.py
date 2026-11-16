"""Optional real inference smoke test, isolated from the user's application data."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
import app


@unittest.skipUnless(
    (app.ROOT / "yolo11n.pt").exists()
    and importlib.util.find_spec("ultralytics")
    and importlib.util.find_spec("cv2"),
    "optional YOLO model/dependencies not installed",
)
class VideoSmokeTests(unittest.TestCase):
    def test_cancel_before_model_loading_releases_worker_lock(self):
        from unittest.mock import patch
        import processing

        with tempfile.TemporaryDirectory() as folder:
            original = app.DATA, app.DB
            app.DATA = Path(folder)
            app.DB = app.DATA / "cancel.db"
            try:
                app.initialize()
                app.JOBS["cancel-smoke"] = {"status": "running", "cancel_requested": True}
                app.ANALYSIS_LOCK.acquire()
                with patch.object(
                    processing, "load_model", side_effect=AssertionError("model must not load")
                ):
                    app.analyze("cancel-smoke", "unused.mp4", app.DEFAULTS)
                self.assertEqual(app.JOBS["cancel-smoke"]["status"], "cancelled")
                self.assertFalse(app.ANALYSIS_LOCK.locked())
            finally:
                app.JOBS.pop("cancel-smoke", None)
                app.DATA, app.DB = original

    def test_real_yolo_on_blank_video_has_no_alerts(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            original = app.DATA, app.DB
            app.DATA = Path(folder)
            app.DB = app.DATA / "smoke.db"
            try:
                app.initialize()
                filename = "blank.avi"
                writer = cv2.VideoWriter(
                    str(app.DATA / "uploads" / filename),
                    cv2.VideoWriter_fourcc(*"MJPG"),
                    5,
                    (320, 240),
                )
                self.assertTrue(writer.isOpened())
                for _ in range(8):
                    writer.write(np.full((240, 320, 3), 70, dtype=np.uint8))
                writer.release()
                job_id = "smoke-test"
                app.JOBS[job_id] = {"status": "running", "progress": 0}
                self.assertTrue(app.ANALYSIS_LOCK.acquire(blocking=False))
                app.analyze(job_id, filename, {**app.DEFAULTS, "dwell_seconds": 1})
                job = app.JOBS[job_id]
                self.assertEqual(job["status"], "complete", job.get("message"))
                self.assertEqual(job["progress"], 100)
                self.assertEqual(job["people"], 0)
                self.assertTrue((app.DATA / job["frame"].removeprefix("/media/")).exists())
                with app.connection() as db:
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) FROM events WHERE mode='real'").fetchone()[0], 0
                    )
            finally:
                app.DATA, app.DB = original
                app.JOBS.pop("smoke-test", None)

    def test_cancel_during_processing_keeps_partial_preview(self):
        from unittest.mock import patch
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            original = app.DATA, app.DB
            app.DATA = Path(folder)
            app.DB = app.DATA / "partial.db"
            try:
                app.initialize()
                writer = cv2.VideoWriter(
                    str(app.DATA / "uploads" / "partial.avi"),
                    cv2.VideoWriter_fourcc(*"MJPG"),
                    5,
                    (320, 240),
                )
                self.assertTrue(writer.isOpened())
                for _ in range(8):
                    writer.write(np.full((240, 320, 3), 70, dtype=np.uint8))
                writer.release()
                app.JOBS["partial-smoke"] = {"status": "running", "progress": 0}
                app.ANALYSIS_LOCK.acquire()
                with patch.object(app, "is_cancelled", side_effect=[False, False, True]):
                    app.analyze("partial-smoke", "partial.avi", app.DEFAULTS)
                job = app.JOBS["partial-smoke"]
                self.assertEqual(job["status"], "cancelled")
                self.assertEqual(job["processed_frames"], 1)
                self.assertTrue((app.DATA / job["frame"].removeprefix("/media/")).exists())
                self.assertFalse(app.ANALYSIS_LOCK.locked())
            finally:
                app.JOBS.pop("partial-smoke", None)
                app.DATA, app.DB = original


if __name__ == "__main__":
    unittest.main()
