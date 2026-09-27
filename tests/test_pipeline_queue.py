import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


class PipelineTests(unittest.TestCase):
    def test_conversion_queues_upload(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.dict(
            os.environ, {"BILI_JOBS_DIR": directory, "BILI_DOWNLOADS_DIR": directory,
                         "BILI_WEB_TOKEN": "test-token"}
        ):
            spec = importlib.util.spec_from_file_location("queue_test_web", SCRIPTS / "bili_web.py")
            web = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(web)
            job = "f" * 16
            meta = Path(directory) / f"{job}.meta.json"
            state = Path(directory) / f"{job}.json"
            log = Path(directory) / f"{job}.log"
            meta.write_text(json.dumps({"kind": "hls", "upload": True, "keep_local": False}))
            state.write_text(json.dumps({"outputs": [str(Path(directory) / "hls" / "film")]}))
            log.touch()
            web.on_job_complete({"job": job, "lane": "convert", "meta": str(meta),
                                 "state": str(state), "log": str(log)}, 0)
            upload = next(item for item in web.jobs.values() if item["lane"] == "upload")
            config = json.loads((Path(directory) / f"{upload['job']}.config.json").read_text())
            self.assertEqual(config["kind"], "hls")
            self.assertEqual(config["paths"], [str(Path(directory) / "hls" / "film")])
            self.assertEqual(json.loads(state.read_text())["upload_job"], upload["job"])

    def test_upload_keeps_bundle_on_failure(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            bundle = root / "hls" / "job" / "film"
            bundle.mkdir(parents=True)
            (bundle / "film.m3u8").write_text("#EXTM3U")
            config = root / "upload.config.json"
            state = root / "upload.json"
            config.write_text(json.dumps({"kind": "hls", "paths": [str(bundle)], "keep_local": False}))
            spec = importlib.util.spec_from_file_location("queue_test_upload", SCRIPTS / "upload_media.py")
            worker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(worker)
            with patch.object(worker, "HLS_ROOT", root / "hls"), patch.object(
                worker, "RCLONE_REMOTE", "remote:target"
            ), patch.object(worker, "run_with_progress", side_effect=RuntimeError("network down")):
                with patch.object(sys, "argv", ["upload_media.py", str(config), str(state)]):
                    with self.assertRaisesRegex(RuntimeError, "network down"):
                        worker.main()
            self.assertTrue(bundle.exists())
            self.assertEqual(json.loads(state.read_text())["status"], "failed")

    def test_upload_removes_bundle_only_after_success(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            bundle = root / "hls" / "job" / "film"
            bundle.mkdir(parents=True)
            (bundle / "film.m3u8").write_text("#EXTM3U")
            config = root / "upload.config.json"
            state = root / "upload.json"
            config.write_text(json.dumps({"kind": "hls", "paths": [str(bundle)], "keep_local": False}))
            spec = importlib.util.spec_from_file_location("queue_success_upload", SCRIPTS / "upload_media.py")
            worker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(worker)
            with patch.object(worker, "HLS_ROOT", root / "hls"), patch.object(
                worker, "RCLONE_REMOTE", "remote:target"
            ), patch.object(worker, "run_with_progress") as transfer:
                with patch.object(sys, "argv", ["upload_media.py", str(config), str(state)]):
                    worker.main()
            self.assertFalse(bundle.exists())
            self.assertEqual(json.loads(state.read_text())["status"], "completed")
            self.assertEqual(transfer.call_args.args[0][0:3], ["rclone", "copy", str(bundle)])


if __name__ == "__main__":
    unittest.main()
