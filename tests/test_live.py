"""Real inference on generated JPEGs; no webcam or private camera access."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
import app
from live import LiveSession


@unittest.skipUnless(
    (app.ROOT / "yolo11n.pt").exists() and importlib.util.find_spec("ultralytics"),
    "optional YOLO dependencies",
)
class LiveInferenceTests(unittest.TestCase):
    def test_jpeg_inference_and_stop_do_not_record_a_video(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as folder:
            old = app.DATA, app.DB
            app.DATA = Path(folder)
            app.DB = app.DATA / "live.db"
            session = None
            try:
                app.initialize()
                job_id = "live-jpeg-smoke"
                app.JOBS[job_id] = {"status": "running", "live": True, "progress": 0}
                app.ANALYSIS_LOCK.acquire()
                session = LiveSession(app, job_id, app.DEFAULTS)
                app.LIVE_SESSIONS[job_id] = session
                session.prepare()
                self.assertTrue(session.ready)
                invalid, code = session.frame(b"invalid jpeg")
                self.assertEqual(code, 400)
                ok, jpeg = cv2.imencode(".jpg", np.full((240, 320, 3), 70, dtype=np.uint8))
                self.assertTrue(ok)
                result, code = session.frame(jpeg.tobytes())
                self.assertEqual(code, 200)
                self.assertTrue(result["ready"])
                self.assertEqual(result["people"], 0)
                self.assertEqual(result["boxes"], [])
                self.assertFalse(any((app.DATA / "uploads").iterdir()))
                with app.connection() as db:
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) FROM events WHERE mode='real'").fetchone()[0], 0
                    )
                session.stop()
                self.assertFalse(app.ANALYSIS_LOCK.locked())
                self.assertEqual(app.JOBS[job_id]["status"], "stopped")
                self.assertEqual(session.frame(jpeg.tobytes())[1], 410)
            finally:
                if session and not session.closed:
                    session.stop()
                app.JOBS.pop("live-jpeg-smoke", None)
                app.DATA, app.DB = old
