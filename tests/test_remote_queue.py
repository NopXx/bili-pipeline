import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from process_media import engine_env  # noqa: E402


class EngineEnvTests(unittest.TestCase):
    def test_web_modes_map_to_hls_prep(self):
        self.assertEqual(engine_env({"copy_video": True, "audio_channels": "2"}),
                         {"PREP_COPY_VIDEO": "1", "PREP_AUDIO_CHANNELS": "2"})
        # hls-prep has no PREP_HEIGHT: a sized encode is a one-rung ladder.
        self.assertEqual(engine_env({"reencode": True, "height": 720, "video_bitrate": "2M"}), {
            "PREP_FORCE_VIDEO_ENCODE": "1", "PREP_LADDER": "1", "PREP_LADDER_HEIGHTS": "720", "PREP_LADDER_BITRATES": "720:2M"})
        self.assertEqual(engine_env({"reencode": True, "height": 0, "video_bitrate": "8M"}),
                         {"PREP_FORCE_VIDEO_ENCODE": "1", "PREP_VIDEO_BITRATE": "8M"})
        ladder = engine_env({"ladder": True, "ladder_heights": "raw,1080,720", "ladder_bitrates": "1080=3M,720:1.5M"})
        self.assertEqual((ladder["PREP_LADDER_BITRATES"], ladder["PREP_FORCE_VIDEO_ENCODE"]), ("1080:3M,720:1.5M", "1"))
        auto = engine_env({"auto_hdr": True, "ladder_bitrates": "2160=16M,1080=8M,720=4M"})
        self.assertEqual((auto["PREP_LADDER_HEIGHTS"], auto["PREP_LADDER_BITRATES"], auto["PREP_GPU_TONEMAP"]),
                         ("raw,1080,720", "1080:8M,720:4M", "1"))
        self.assertEqual(engine_env({"preserve_hdr": True, "video_bitrate": "15M"}),
                         {"PREP_LADDER": "1", "PREP_LADDER_HEIGHTS": "hdr", "PREP_HDR_BITRATE": "15M"})


class RemoteQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        root = Path(self.directory.name)
        self.env = patch.dict(os.environ, {"BILI_JOBS_DIR": str(root / "jobs"), "BILI_DOWNLOADS_DIR": str(root / "downloads"),
                                           "BILI_HLS_DIR": str(root / "hls"), "BILI_WEB_TOKEN": "t",
                                           "BILI_RCLONE_REMOTE": "metube:tube", "BILI_TRANSFER_ONLY": "0"})
        self.env.start()
        spec = importlib.util.spec_from_file_location("remote_web", SCRIPTS / "bili_web.py")
        self.web = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.web)

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def test_paths_cannot_escape_the_remote(self):
        self.assertEqual(self.web.remote_relative("/Movie//Movie.mkv"), "Movie/Movie.mkv")
        with self.assertRaises(ValueError):
            self.web.remote_relative("Movie/../../other")

    def test_download_convert_upload_then_delete_remote_original(self):
        web = self.web
        pipeline = {"hls": {"ladder": True, "ladder_heights": "1080,720"}, "delete_remote_source": True}
        download = web.enqueue_remote_download("Movie/Movie.mkv", pipeline)
        config = json.loads(Path(web.JOBS_DIR, download + ".config.json").read_text())
        self.assertEqual(config["source"], "metube:tube/Movie/Movie.mkv")

        # The download finishes: its file is converted with the chosen profile.
        local = Path(config["destination"]) / "Movie.mkv"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"video")
        web.queue._state(web.jobs[download], video_files=[str(local)])
        web.on_job_complete(web.jobs[download], 0)
        convert = json.loads(Path(web.jobs[download]["state"]).read_text())["convert_job"]
        convert_meta = json.loads(Path(web.JOBS_DIR, convert + ".meta.json").read_text())
        self.assertEqual((convert_meta["ladder_heights"], convert_meta["upload"]), ("1080,720", True))
        self.assertEqual(convert_meta["pipeline"]["remote_source"], "Movie/Movie.mkv")

        # Conversion done: upload queued with the bundle.
        bundle = Path(os.environ["BILI_HLS_DIR"], convert, "Movie")
        web.queue._state(web.jobs[convert], outputs=[str(bundle)])
        web.on_job_complete(web.jobs[convert], 0)
        upload = json.loads(Path(web.jobs[convert]["state"]).read_text())["upload_job"]

        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            stdout = "Movie.m3u8\n" if command[1] == "lsf" and self.playlist_uploaded else ""
            return subprocess.CompletedProcess(command, 0, stdout, "")

        # Playlist missing on the remote: the original must be kept.
        self.playlist_uploaded = False
        with patch.object(web.subprocess, "run", fake_run):
            web.finish_remote_pipeline(web.jobs[upload], convert_meta["pipeline"], [str(bundle)])
        self.assertFalse(local.exists())  # local original always cleaned up
        self.assertEqual([c[1] for c in calls], ["lsf"])
        self.assertEqual(calls[0][2], "metube:tube/Movie")

        self.playlist_uploaded = True
        calls.clear()
        with patch.object(web.subprocess, "run", fake_run):
            web.finish_remote_pipeline(web.jobs[upload], convert_meta["pipeline"], [str(bundle)])
        self.assertEqual(calls[-1], ["rclone", "deletefile", "metube:tube/Movie/Movie.mkv"])
        self.assertIn("deleted remote original", Path(web.jobs[upload]["log"]).read_text())


if __name__ == "__main__":
    unittest.main()
