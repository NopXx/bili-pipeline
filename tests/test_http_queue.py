import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


class HttpQueueTests(unittest.TestCase):
    def test_upload_pause_resume_cancel(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.dict(
            os.environ, {"BILI_JOBS_DIR": str(Path(directory) / "jobs"),
                         "BILI_DOWNLOADS_DIR": str(Path(directory) / "downloads"),
                         "BILI_WEB_TOKEN": "test-token"}
        ):
            spec = importlib.util.spec_from_file_location("http_queue_web", SCRIPTS / "bili_web.py")
            web = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(web)
            source = Path(directory) / "downloads" / "film.mkv"
            source.parent.mkdir(exist_ok=True)
            source.write_bytes(b"sample")
            server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def post(route, body):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}{route}",
                    data=json.dumps(body).encode(),
                    headers={"X-Token": "test-token", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.load(response)

            try:
                job = post("/api/upload", {"files": [str(source)]})["job"]
                self.assertEqual(post("/api/status", {"job": job})["state"]["status"], "queued")
                self.assertEqual(post("/api/pause", {"job": job})["status"], "paused")
                self.assertEqual(post("/api/status", {"job": job})["state"]["status"], "paused")
                self.assertEqual(post("/api/resume", {"job": job})["status"], "queued")
                self.assertEqual(post("/api/cancel", {"job": job})["cancelled"], True)
                self.assertEqual(post("/api/jobs", {})[0]["lane"], "upload")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class FileUploadTests(unittest.TestCase):
    def post_raw(self, port, name, data, token="test-token"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/files/upload", data=data, method="POST",
            headers={"X-Token": token, "X-Filename": name},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_upload_roundtrip_and_guards(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.dict(
            os.environ, {"BILI_JOBS_DIR": str(Path(directory) / "jobs"),
                         "BILI_DOWNLOADS_DIR": str(Path(directory) / "downloads"),
                         "BILI_WEB_TOKEN": "test-token"}
        ):
            spec = importlib.util.spec_from_file_location("upload_web", SCRIPTS / "bili_web.py")
            web = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(web)
            server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_port
                payload = b"fake-video-bytes" * 1000
                code, body = self.post_raw(port, "Film%20Name.mkv", payload)
                self.assertEqual(code, 200)
                saved = Path(body["path"])
                self.assertEqual(saved.read_bytes(), payload)
                self.assertTrue(str(saved).startswith(str(Path(directory) / "downloads")))
                self.assertEqual(saved.name, "Film Name.mkv")
                # Duplicate name gets a counter suffix, not an overwrite.
                code, body2 = self.post_raw(port, "Film%20Name.mkv", b"second")
                self.assertEqual(code, 200)
                self.assertEqual(body2["name"], "Film Name (2).mkv")
                # Path traversal collapses to a basename inside uploads/.
                code, body3 = self.post_raw(port, "..%2F..%2Fevil.mkv", b"x")
                self.assertEqual(code, 200)
                self.assertEqual(Path(body3["path"]).parent.name, "uploads")
                self.assertNotIn("..", body3["name"])
                # Non-media extensions and empty bodies are refused.
                self.assertEqual(self.post_raw(port, "run.exe", b"x")[0], 400)
                self.assertEqual(self.post_raw(port, "film.mkv", b"")[0], 411)
                self.assertEqual(self.post_raw(port, "film.mkv", b"x", token="wrong")[0], 401)
                # Uploaded files show up in the downloads listing.
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/files/downloads", data=b"{}",
                    headers={"X-Token": "test-token", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    names = [item["relative"] for item in json.load(response)]
                self.assertIn(str(Path("uploads") / "Film Name.mkv"), names)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class VideoAspectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("aspect_web", SCRIPTS / "bili_web.py")
        cls.web = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.web)

    def test_imax_expanded_ratio(self):
        aspect, imax = self.web.video_aspect({"display_aspect_ratio": "480:253"}, 3840, 2024)
        self.assertEqual(aspect, 1.897)
        self.assertTrue(imax)

    def test_scope_and_flat_are_not_imax(self):
        for stream, w, h in (({"display_aspect_ratio": "239:100"}, 0, 0),
                             ({"width": 3840, "height": 2076}, 3840, 2076),
                             ({"width": 320, "height": 180}, 320, 180)):
            _, imax = self.web.video_aspect(stream, w, h)
            self.assertFalse(imax)

    def test_no_video_stream(self):
        self.assertEqual(self.web.video_aspect({}, 0, 0), (None, False))
        self.assertEqual(self.web.video_aspect({"display_aspect_ratio": "0:0"}, 0, 0), (None, False))


if __name__ == "__main__":
    unittest.main()
