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


if __name__ == "__main__":
    unittest.main()
