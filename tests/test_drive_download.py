import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


class DriveDownloadTests(unittest.TestCase):
    def test_drive_link_enters_download_queue(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.dict(
            os.environ, {"BILI_JOBS_DIR": str(Path(directory) / "jobs"),
                         "BILI_DOWNLOADS_DIR": str(Path(directory) / "downloads"),
                         "BILI_WEB_TOKEN": "test-token"}
        ):
            spec = importlib.util.spec_from_file_location("drive_download_web", SCRIPTS / "bili_web.py")
            web = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(web)
            server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def post(route, body):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}{route}",
                    data=json.dumps(body).encode(),
                    headers={"X-Token": "test-token", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.load(response)

            try:
                link = "https://drive.google.com/file/d/1234567890abcdefgh/view?resourcekey=0-abcDEF123"
                job = post("/api/drive/download", {"source": link})["job"]
                self.assertEqual(web.jobs[job]["lane"], "download")
                config = json.loads((Path(directory) / "jobs" / f"{job}.config.json").read_text())
                self.assertEqual(config["file_id"], "1234567890abcdefgh")
                self.assertEqual(config["resource_key"], "0-abcDEF123")
                self.assertEqual(post("/api/status", {"job": job})["state"]["status"], "queued")
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    post("/api/drive/download", {"source": "https://drive.google.com/drive/folders/1234567890abcdefgh"})
                self.assertEqual(failure.exception.code, 400)
                retry = post("/api/retry", {"job": job})["job"]
                self.assertEqual(web.jobs[retry]["lane"], "download")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
