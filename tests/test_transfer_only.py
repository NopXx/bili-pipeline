import base64
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


class TransferOnlyTests(unittest.TestCase):
    def test_transfer_page_and_conversion_guard(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.dict(
            os.environ, {"BILI_JOBS_DIR": str(Path(directory) / "jobs"),
                         "BILI_DOWNLOADS_DIR": str(Path(directory) / "downloads"),
                         "BILI_WEB_TOKEN": "test-token", "BILI_TRANSFER_ONLY": "1"}
        ):
            spec = importlib.util.spec_from_file_location("transfer_only_web", SCRIPTS / "bili_web.py")
            web = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(web)
            source = Path(directory) / "downloads" / "film.mkv"
            source.parent.mkdir(exist_ok=True)
            source.write_bytes(b"sample")
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
                page = urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5).read().decode()
                self.assertIn("Download / Upload", page)
                self.assertTrue(post("/api/health", {})["transfer_only"])
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    post("/api/process", {"files": [str(source)]})
                self.assertEqual(failure.exception.code, 403)
                job = post("/api/upload", {"files": [str(source)]})["job"]
                self.assertEqual(web.jobs[job]["lane"], "upload")
                tail = post("/api/log", {"job": job})
                self.assertIn("queued in upload lane", tail["text"])
                self.assertEqual(tail["offset"], tail["size"])
                with open(web.jobs[job]["log"], "a", encoding="utf-8", newline="") as log:
                    log.write("ไฟล์ 1\npartial")
                follow = post("/api/log", {"job": job, "offset": tail["offset"]})
                self.assertEqual(follow["text"], "ไฟล์ 1\n")  # the unfinished line waits
                self.assertEqual(post("/api/log", {"job": job, "offset": follow["offset"]})["text"], "")
                self.assertTrue(post("/api/log", {"job": job, "offset": 10 ** 9})["reset"])
                older = post("/api/log", {"job": job, "before": follow["start"]})
                self.assertEqual(older["start"], 0)
                self.assertEqual(older["text"], tail["text"])
                inspection = post("/api/torrent/inspect", {
                    "torrent_data": base64.b64encode(b"d3:foo3:bare").decode()
                })["job"]
                self.assertEqual(web.jobs[inspection]["lane"], "inspect")
                torrent_data = base64.b64encode(b"d4:infod4:name4:test6:lengthi4eeee").decode()
                torrent = post("/api/torrent", {"torrent_data": torrent_data})["job"]
                original_destination = web.jobs[torrent]["command"][3]
                with self.assertRaises(urllib.error.HTTPError) as active_retry:
                    post("/api/retry", {"job": torrent})
                self.assertEqual(active_retry.exception.code, 409)
                web.jobs[torrent]["status"] = "failed"
                retried = post("/api/retry", {"job": torrent})["job"]
                self.assertNotEqual(retried, torrent)
                self.assertEqual(web.jobs[retried]["command"][3], original_destination)
                with self.assertRaises(urllib.error.HTTPError) as duplicate_retry:
                    post("/api/retry", {"job": torrent})
                self.assertEqual(duplicate_retry.exception.code, 409)
                inside = Path(original_destination) / "test.mkv"
                inside.parent.mkdir(parents=True)
                inside.write_bytes(b"downloaded")
                protected = post("/api/delete/downloads", {"paths": [str(source), str(inside)]})
                self.assertEqual(protected, {"deleted": 0, "failed": 2})
                self.assertTrue(source.exists())
                self.assertTrue(inside.exists())
                web.jobs[job]["status"] = "completed"
                web.jobs[retried]["status"] = "failed"
                outside = Path(directory) / "outside.txt"
                outside.write_text("keep")
                deleted = post("/api/delete/downloads", {"paths": [str(inside), str(outside)]})
                self.assertEqual(deleted, {"deleted": 1, "failed": 1})
                self.assertFalse(inside.exists())
                self.assertTrue(outside.exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
