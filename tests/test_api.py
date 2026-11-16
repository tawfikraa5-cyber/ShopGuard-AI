import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer
import app


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.previous = app.DATA, app.DB
        app.DATA = Path(cls.temp.name)
        app.DB = app.DATA / "test.db"
        app.initialize()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()
        app.DATA, app.DB = cls.previous
        cls.temp.cleanup()

    def request(self, path, body=None, headers=None):
        req = Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_demo_is_separate_from_real(self):
        self.assertEqual(len(self.request("/api/state?mode=demo")["events"]), 8)
        self.assertEqual(self.request("/api/state?mode=real")["events"], [])

    def test_review_persists_and_false_positive_clears_loss(self):
        self.request("/api/review", {"id": "demo-1", "status": "confirmed", "actual": 42.5})
        event = next(
            e for e in self.request("/api/state?mode=demo")["events"] if e["id"] == "demo-1"
        )
        self.assertEqual(event["actual"], 42.5)
        self.request("/api/review", {"id": "demo-1", "status": "false_positive", "actual": 42.5})
        event = next(
            e for e in self.request("/api/state?mode=demo")["events"] if e["id"] == "demo-1"
        )
        self.assertEqual(event["actual"], 0)
        self.assertEqual(event["status"], "false_positive")

    def test_invalid_zone_rejected_without_changing_settings(self):
        before = self.request("/api/state")["settings"]["zone"]
        with self.assertRaises(HTTPError) as error:
            self.request("/api/settings", {"zone": [0.8, 0, 0.2, 1]})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.request("/api/state")["settings"]["zone"], before)

    def test_cross_origin_write_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.request(
                "/api/settings", {"shop_name": "changed"}, {"Origin": "https://example.com"}
            )
        self.assertEqual(error.exception.code, 403)

    def test_upload_rejects_executable_extension(self):
        req = Request(self.url + "/api/upload", data=b"bad", headers={"X-Filename": "bad.exe"})
        with self.assertRaises(HTTPError) as error:
            urlopen(req, timeout=5)
        self.assertEqual(error.exception.code, 400)

    def test_video_range_is_streamed_and_invalid_range_rejected(self):
        (app.DATA / "uploads" / "range.mp4").write_bytes(b"0123456789")
        req = Request(self.url + "/media/uploads/range.mp4", headers={"Range": "bytes=2-5"})
        with urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
            self.assertEqual(response.read(), b"2345")
        req = Request(self.url + "/media/uploads/range.mp4", headers={"Range": "bytes=30-50"})
        with self.assertRaises(HTTPError) as error:
            urlopen(req, timeout=5)
        self.assertEqual(error.exception.code, 416)

    def test_completed_job_survives_memory_reset(self):
        app.JOBS["persist-test"] = {"status": "running"}
        app.set_job("persist-test", status="complete", progress=100, source_name="test.mp4")
        del app.JOBS["persist-test"]
        self.assertEqual(self.request("/api/jobs/persist-test")["progress"], 100)
        self.assertTrue(any(r["id"] == "persist-test" for r in self.request("/api/runs")))

    def test_invalid_analysis_profile_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/settings", {"analysis_profile": "invalid"})
        self.assertEqual(error.exception.code, 400)

    def test_review_undo_restores_previous_decision_and_amount(self):
        before = next(
            e for e in self.request("/api/state?mode=demo")["events"] if e["id"] == "demo-5"
        )
        saved = self.request(
            "/api/review",
            {
                "id": "demo-5",
                "status": "confirmed",
                "actual": 35,
                "note": "test",
                "expected_status": before["status"],
            },
        )
        result = self.request("/api/review/undo", {"review_id": saved["review_id"]})
        self.assertEqual(result["event_id"], "demo-5")
        after = next(
            e for e in self.request("/api/state?mode=demo")["events"] if e["id"] == "demo-5"
        )
        self.assertEqual(
            [after[k] for k in ("status", "actual", "note")],
            [before[k] for k in ("status", "actual", "note")],
        )
        with self.assertRaises(HTTPError) as error:
            self.request("/api/review/undo", {"review_id": saved["review_id"]})
        self.assertEqual(error.exception.code, 409)

    def test_stale_tab_and_old_undo_cannot_overwrite_newer_review(self):
        first = self.request("/api/review", {"id": "demo-6", "status": "confirmed", "actual": 5})
        self.request(
            "/api/review",
            {"id": "demo-6", "status": "false_positive", "expected_status": "confirmed"},
        )
        with self.assertRaises(HTTPError) as error:
            self.request(
                "/api/review", {"id": "demo-6", "status": "confirmed", "expected_status": "pending"}
            )
        self.assertEqual(error.exception.code, 409)
        with self.assertRaises(HTTPError) as error:
            self.request("/api/review/undo", {"review_id": first["review_id"]})
        self.assertEqual(error.exception.code, 409)

    def test_cancel_requests_are_cooperative(self):
        app.JOBS["cancel-test"] = {"status": "running"}
        try:
            self.request("/api/cancel", {"id": "cancel-test"})
            self.assertTrue(app.is_cancelled("cancel-test"))
        finally:
            app.JOBS.pop("cancel-test", None)

    def test_private_database_and_path_escape_are_not_served(self):
        for path in ["/media/shopguard.db", "/media/uploads/../test.db"]:
            with self.assertRaises(HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, 404)

    def test_export_is_scoped_and_formula_safe(self):
        app.review_event("demo-3", "confirmed", 0, "=SUM(A1:A2)")
        with urlopen(self.url + "/api/export?mode=demo&run_id=demo", timeout=5) as response:
            text = response.read().decode("utf-8-sig")
        self.assertIn("'=SUM(A1:A2)", text)
        self.assertIn("demo-3", text)
        with urlopen(self.url + "/api/export?mode=real", timeout=5) as response:
            self.assertNotIn("demo-3", response.read().decode())

    def test_unfinished_checkpoint_is_marked_interrupted_on_restart(self):
        path = app.DATA / "jobs" / "checkpoint-test.json"
        app.atomic_json(path, {"status": "running", "progress": 37})
        app.initialize()
        job = self.request("/api/jobs/checkpoint-test")
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(job["progress"], 37)

    def test_duplicate_upload_reuses_stored_video(self):
        def upload():
            request = Request(
                self.url + "/api/upload",
                data=b"fixture-video-bytes",
                headers={"X-Filename": "test.mp4"},
            )
            with urlopen(request, timeout=5) as response:
                return json.load(response)

        first, second = upload(), upload()
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["filename"], second["filename"])
        self.assertEqual(
            (app.DATA / "uploads" / first["filename"]).read_bytes(), b"fixture-video-bytes"
        )

    def test_per_job_options_do_not_race_with_shared_settings(self):
        from unittest.mock import patch

        finished = threading.Event()
        captured = {}
        (app.DATA / "uploads" / "options.mp4").write_bytes(b"fixture")
        before = self.request("/api/state")["settings"]

        def fake(job_id, filename, config):
            try:
                captured.update(config)
                app.set_job(job_id, status="complete", progress=100)
            finally:
                app.ANALYSIS_LOCK.release()
                finished.set()

        with patch.object(app, "analyze", side_effect=fake):
            self.request(
                "/api/analyze",
                {
                    "filename": "options.mp4",
                    "analysis_options": {"dwell_seconds": 3, "analysis_profile": "fast"},
                },
            )
            self.assertTrue(finished.wait(5))
        self.assertEqual(captured["dwell_seconds"], 3)
        self.assertEqual(captured["analysis_profile"], "fast")
        self.assertEqual(self.request("/api/state")["settings"], before)

    def test_live_session_stop_releases_analysis_slot(self):
        from unittest.mock import patch
        from live import LiveSession

        with patch.object(LiveSession, "start", return_value=None):
            result = self.request("/api/live/start", {"analysis_options": {"dwell_seconds": 3}})
        session_id = result["id"]
        try:
            self.assertTrue(app.ANALYSIS_LOCK.locked())
            req = Request(
                self.url + "/api/live/frame?id=" + session_id,
                data=b"frame",
                headers={"Content-Type": "image/jpeg"},
            )
            with urlopen(req, timeout=5) as response:
                self.assertFalse(json.load(response)["ready"])
            self.request("/api/live/stop", {"id": session_id})
            self.assertFalse(app.ANALYSIS_LOCK.locked())
            self.assertEqual(self.request("/api/jobs/" + session_id)["status"], "stopped")
            self.request("/api/live/stop", {"id": session_id})
        finally:
            session = app.LIVE_SESSIONS.get(session_id)
            if session:
                session.stop()

    def test_live_start_refuses_an_active_file_analysis(self):
        app.ANALYSIS_LOCK.acquire()
        try:
            with self.assertRaises(HTTPError) as error:
                self.request("/api/live/start", {})
            self.assertEqual(error.exception.code, 409)
        finally:
            app.ANALYSIS_LOCK.release()


if __name__ == "__main__":
    unittest.main()
