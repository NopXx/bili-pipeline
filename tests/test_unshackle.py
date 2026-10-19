import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class FakeUnshackle:
    """Minimal unshackle REST stand-in: one job that downloads, then completes (or fails)."""

    def __init__(self, destination=None, fail=False):
        self.requests, self.polls, self.deleted = [], 0, False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.headers.get("X-Secret-Key") != "k":
                    return self._json(401, {"message": "bad key"})
                if self.path == "/api/services":
                    return self._json(200, {"services": [{"tag": "DSNP", "has_search": True}, {"tag": "AMZN"}, {"tag": "NF"}]})
                fake.polls += 1
                if fake.polls < 2:
                    return self._json(200, {"status": "downloading", "progress": 42.5, "phase": "downloading video", "speed": "9 MB/s"})
                if fail:
                    return self._json(200, {"status": "failed", "error_message": "There's no 2160p Video Track..."})
                Path(destination, "Film.2160p.mkv").write_bytes(b"x")
                return self._json(200, {"status": "completed", "progress": 100, "output_files": ["Downloads/Film.2160p.mkv"]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if self.path == "/api/search":
                    return self._json(200, {"results": [{"id": "entity-1", "title": "Film", "label": "2021"}]})
                if self.path == "/api/list-titles":
                    return self._json(200, {"titles": [{"type": "episode", "season": 1, "number": 1, "name": "Pilot"}]})
                if self.path == "/api/list-tracks":
                    if body.get("title_id") == "bad":
                        return self._json(404, {"message": "Title not found"})
                    return self._json(200, {"video": [{"id": "v1", "height": 2160}], "audio": [], "subtitles": []})
                self._json(200, {"job_id": "uj1", "status": "queued"})

            def do_DELETE(self):
                fake.deleted = True
                self._json(200, {"status": "success"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


class UnshackleRouteTests(unittest.TestCase):
    def test_tab_queues_a_worker_with_a_clean_request(self):
        fake = FakeUnshackle()
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.dict(
            os.environ, {"BILI_JOBS_DIR": str(Path(directory) / "jobs"), "BILI_DOWNLOADS_DIR": str(Path(directory) / "downloads"),
                         "BILI_WEB_TOKEN": "t", "BILI_TRANSFER_ONLY": "1", "UNSHACKLE_URL": fake.url, "UNSHACKLE_KEY": "k"}
        ):
            spec = importlib.util.spec_from_file_location("unshackle_web", SCRIPTS / "bili_web.py")
            web = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(web)
            server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.H)
            threading.Thread(target=server.serve_forever, daemon=True).start()

            def post(route, body):
                request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{route}", data=json.dumps(body).encode(),
                                                 headers={"X-Token": "t", "Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.load(response)

            try:
                # Only services verified on unshackle 6.0 are offered (NF exists upstream but is hidden).
                self.assertEqual(post("/api/unshackle/services", {}),
                                 {"enabled": True, "services": ["AMZN", "DSNP"], "searchable": ["DSNP"]})
                self.assertEqual(post("/api/unshackle/search", {"service": "DSNP", "query": "film"})["results"][0]["id"], "entity-1")
                self.assertEqual(post("/api/unshackle/titles", {"service": "DSNP", "title": "entity-1"})["titles"][0]["name"], "Pilot")
                post("/api/unshackle/tracks", {"service": "DSNP", "title": "entity-1", "wanted": "S01E01", "range": ["DV", "bogus"],
                                               "proxy": "socks5://u:p@h:1"})
                self.assertEqual(fake.requests[-1], {"service": "DSNP", "title_id": "entity-1", "wanted": "S01E01",
                                                     "range": ["DV"], "proxy": "socks5://u:p@h:1"})
                with self.assertRaises(urllib.error.HTTPError) as missing:  # unshackle's message reaches the UI
                    post("/api/unshackle/tracks", {"service": "DSNP", "title": "bad"})
                self.assertEqual(missing.exception.code, 502)
                self.assertIn("Title not found", missing.exception.read().decode())
                with self.assertRaises(urllib.error.HTTPError) as hidden:
                    post("/api/unshackle/titles", {"service": "NF", "title": "1"})
                self.assertEqual(hidden.exception.code, 400)
                with self.assertRaises(urllib.error.HTTPError) as no_audio:  # blank audio would pull every dub
                    post("/api/unshackle/download", {"service": "DSNP", "title": "entity-1", "a_lang": " "})
                self.assertEqual(no_audio.exception.code, 400)
                job = post("/api/unshackle/download", {"service": "DSNP", "title": "entity-1", "quality": "2160", "vbitrate": "16932",
                                                       "range": ["DV"], "a_lang": ["en", "orig"], "s_lang": [],
                                                       "wanted": ["S01E01", "S01E02"], "proxy": "socks5://u:p@h:1"})["job"]
                item = web.jobs[job]
                self.assertEqual((item["lane"], item["command"][1]), ("download", web.UNSHACKLE_PULL))
                self.assertEqual(item["env_overrides"], {})  # the key must not be persisted with the job
                config = json.loads(Path(item["command"][2]).read_text())
                self.assertEqual(config, {"service": "DSNP", "title_id": "entity-1", "quality": [2160], "range": ["DV"],
                                          "vbitrate": 16932, "a_lang": ["en", "orig"], "no_subs": True,
                                          "wanted": ["S01E01", "S01E02"], "proxy": "socks5://u:p@h:1",
                                          "destination": os.path.join(web.DOWNLOADS_DIR, "unshackle")})
                # A queued job may wait paused; a running one cannot (stopping the worker would not stop unshackle).
                with self.assertRaises(RuntimeError):
                    web.H._bili_tasks(None, item, "pause")
                self.assertIsNone(web.H._bili_tasks(None, item, "cancel"))  # the worker cancels on SIGTERM
            finally:
                server.shutdown()
                fake.server.shutdown()


class UnshackleWorkerTests(unittest.TestCase):
    def run_worker(self, fail):
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "downloads", "unshackle")
            os.makedirs(destination)
            fake = FakeUnshackle(destination, fail=fail)
            config, state = os.path.join(directory, "c.json"), os.path.join(directory, "s.json")
            Path(config).write_text(json.dumps({"service": "DSNP", "title_id": "entity-1", "destination": destination}))
            Path(state).write_text(json.dumps({"status": "running"}))
            with patch.dict(os.environ, {"UNSHACKLE_URL": fake.url, "UNSHACKLE_KEY": "k", "UNSHACKLE_POLL_SECONDS": "0.01"}):
                spec = importlib.util.spec_from_file_location("unshackle_pull", SCRIPTS / "unshackle_pull.py")
                worker = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(worker)
                code = worker.main(config, state)
            fake.server.shutdown()
            return code, json.loads(Path(state).read_text()), fake.requests[0], destination

    def test_completed_job_reports_files_under_destination(self):
        code, state, request, destination = self.run_worker(fail=False)
        self.assertEqual(code, 0)
        self.assertNotIn("output_dir", request)  # unshackle 6.0 rejects one outside its downloads dir
        self.assertEqual(state["status"], "running")  # the queue owns status
        self.assertEqual((state["progress"], state["unshackle_job"]), (100, "uj1"))
        self.assertEqual(state["files"], [os.path.join(destination, "Film.2160p.mkv")])

    def test_failed_job_surfaces_the_error(self):
        code, state, _, _ = self.run_worker(fail=True)
        self.assertEqual(code, 1)
        self.assertIn("no 2160p", state["error"])


if __name__ == "__main__":
    unittest.main()
