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


class RcloneProgressTests(unittest.TestCase):
    def test_upload_progress_from_old_and_new_rclone_formats(self):
        from process_media import run_with_progress
        lines = [
            # Ubuntu's older rclone: no space before the unit, "GBytes", "MBytes/s".
            "2026/09/27 09:50:01 NOTICE:       992M / 13.341 GBytes, 7%, 49.932 MBytes/s, ETA 4m13s",
            "2026/09/27 NOTICE:   208.382 MiB / 8.501 GiB, 2%, 32.095 MiB/s, ETA 4m24s",
        ]
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state = Path(directory) / "state.json"
            for line, expected in zip(lines, [(7, "49.932 MBytes/s", "992M", "4m13s"), (2, "32.095 MiB/s", "208.382 MiB", "4m24s")]):
                state.write_text("{}")
                run_with_progress([sys.executable, "-c", f"print({line!r})"], dict(os.environ), 0, "UPLOAD", str(state))
                data = json.loads(state.read_text())
                self.assertEqual((data["progress"], data["speed"], data["done"], data["eta"]), expected)


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

    def test_torrent_auto_upload_queues_source_upload_that_removes_files(self):
        web = self.web
        sent = []
        handler = web.H.__new__(web.H)
        handler._send = lambda code, body: sent.append((code, json.loads(body)))
        with patch.object(web.queue, "submit"):
            handler.handle_torrent({"source": "magnet:?xt=urn:btih:" + "a" * 40, "upload_source": True})
        code, body = sent[0]
        self.assertEqual(code, 200, body)
        job = body["job"]
        meta = json.loads(Path(web.JOBS_DIR, job + ".meta.json").read_text())
        self.assertEqual(meta["pipeline"], {"upload_source": True, "remove_local": True})

        # aria2 saved a folderless two-episode torrent into the job's destination.
        folder = Path(meta["destination"])
        folder.mkdir(parents=True)
        videos = [folder / "E01.mkv", folder / "E02.mkv"]
        for video in videos:
            video.write_bytes(b"video")
        (folder / "info.nfo").write_text("x")

        item = {"job": job, "lane": "download", "meta": str(Path(web.JOBS_DIR, job + ".meta.json")),
                "state": str(Path(web.JOBS_DIR, job + ".json")), "log": str(Path(web.JOBS_DIR, job + ".log"))}
        Path(item["state"]).write_text(json.dumps({"video_files": [str(v) for v in videos]}))
        with patch.object(web.queue, "submit"), patch.object(web.queue, "_state") as state:
            web.on_job_complete(item, 0)
        upload = state.call_args.kwargs["upload_job"]
        config = json.loads(Path(web.JOBS_DIR, upload + ".config.json").read_text())
        self.assertEqual((config["kind"], config["paths"], config["remove_source"]), ("source", [str(v) for v in videos], True))
        # Several videos without a torrent folder: one Drive folder named after the torrent.
        self.assertEqual(config["remote_dirs"], [meta["name"]] * 2)

        # upload_media deletes each video only after its own upload succeeded.
        state_path = Path(web.JOBS_DIR, upload + ".json")
        state_path.write_text("{}")
        import upload_media
        uploaded = []
        with patch.object(upload_media, "DOWNLOADS", Path(os.environ["BILI_DOWNLOADS_DIR"]).resolve()),              patch.object(upload_media, "RCLONE_REMOTE", "metube:tube"),              patch.object(upload_media, "run_with_progress", lambda command, *rest: uploaded.append((command[2], Path(command[2]).exists(), command[3]))),              patch.object(sys, "argv", ["upload_media.py", str(Path(web.JOBS_DIR, upload + ".config.json")), str(state_path)]):
            upload_media.main()
        self.assertEqual(uploaded, [(str(v), True, f"metube:tube/{meta['name']}/{v.name}") for v in videos])
        self.assertEqual([v.exists() for v in videos], [False, False])
        self.assertTrue((folder / "info.nfo").exists())

    def test_series_keep_their_folders_on_drive(self):
        web = self.web
        # HLS bundle goes next to the original, wherever it is.
        self.assertEqual(web.hls_remote_dir("Movie/Movie.mkv", "Movie"), "Movie")
        self.assertEqual(web.hls_remote_dir("Show/S1/E01.mkv", "E01"), "Show/S1/E01")
        self.assertEqual(web.hls_remote_dir("E01.mkv", "E01"), "E01")
        # Torrent videos keep the torrent's folders; a folderless torrent gets one named after it.
        destination = os.path.realpath(web.TORRENT_DOWNLOADS_DIR + "/Show-abc123")
        self.assertEqual(web.torrent_remote_dir(destination + "/Show/S1/E01.mkv", destination, "Show"), "Show/S1")
        self.assertEqual(web.torrent_remote_dir(destination + "/E01.mkv", destination, "Show"), "Show")

        # A converted episode uploads into Show/S1/E01 and that is where the playlist is checked.
        bundle = Path(os.environ["BILI_HLS_DIR"], "job", "E01")
        meta = {"upload": True, "pipeline": {"remote_source": "Show/S1/E01.mkv"}}
        item = {"job": "c" * 16, "lane": "convert", "meta": str(Path(web.JOBS_DIR, "c.meta.json")),
                "state": str(Path(web.JOBS_DIR, "c.json")), "log": str(Path(web.JOBS_DIR, "c.log"))}
        Path(item["meta"]).write_text(json.dumps(meta))
        Path(item["state"]).write_text(json.dumps({"outputs": [str(bundle)]}))
        with patch.object(web.queue, "submit"), patch.object(web.queue, "_state") as state:
            web.on_convert_complete(item, 0)
        upload = state.call_args.kwargs["upload_job"]
        config = json.loads(Path(web.JOBS_DIR, upload + ".config.json").read_text())
        self.assertEqual(config["remote_dirs"], ["Show/S1/E01"])
        calls = []
        with patch.object(web.subprocess, "run", lambda command, **kw: calls.append(command) or subprocess.CompletedProcess(command, 0, "", "")):
            web.finish_remote_pipeline(item, {"remote_source": "Show/S1/E01.mkv", "delete_remote_source": True}, [str(bundle)], ["Show/S1/E01"])
        self.assertEqual(calls[0][2], "metube:tube/Show/S1/E01")

    def test_recursive_list_selects_series_videos_and_flags_converted(self):
        web = self.web
        listing = [
            {"Path": "S1 EP-01/S1 EP-01.m3u8", "Name": "S1 EP-01.m3u8", "Size": 1},
            {"Path": "S1 EP-01/S1 EP-01.mkv", "Name": "S1 EP-01.mkv", "Size": 10},
            {"Path": "S1 EP-02/S1 EP-02.mkv", "Name": "S1 EP-02.mkv", "Size": 20},
            {"Path": "S2/E01.mkv", "Name": "E01.mkv", "Size": 30},
            {"Path": "S2/E01/E01.m3u8", "Name": "E01.m3u8", "Size": 1},
            {"Path": "S2/E02.mkv", "Name": "E02.mkv", "Size": 40},
            {"Path": "Old/Old.m3u8", "Name": "Old.m3u8", "Size": 1},
            {"Path": "Old/seg0.ts", "Name": "seg0.ts", "Size": 5},
        ]
        sent = []
        handler = web.H.__new__(web.H)
        handler._send = lambda code, body: sent.append((code, json.loads(body)))
        run = lambda command, **kw: subprocess.CompletedProcess(command, 0, json.dumps(listing), "")
        with patch.object(web.subprocess, "run", run):
            handler.handle_remote_list({"path": "Show", "recursive": True})
        code, body = sent[0]
        self.assertEqual(code, 200, body)
        self.assertEqual({i["path"]: i["converted"] for i in body["items"]}, {
            "Show/S1 EP-01/S1 EP-01.mkv": True, "Show/S1 EP-02/S1 EP-02.mkv": False,
            "Show/S2/E01.mkv": True, "Show/S2/E02.mkv": False})

    def test_pages_pin_assets_to_their_content(self):
        web = self.web
        page = '<script src="/assets/studio.js"></script><link href="/assets/missing.css">'
        pinned = web.ASSET_REF.sub(lambda m: m.group(0) + "?v=" + web.asset_version(m.group(1)), page)
        self.assertIn('/assets/studio.js?v=' + web.asset_version("studio.js") + '"', pinned)
        self.assertRegex(web.asset_version("studio.js"), r"^[0-9a-f]{10}$")
        self.assertIn('/assets/missing.css?v=0"', pinned)

    def test_failed_upload_can_be_retried(self):
        web = self.web
        video = Path(os.environ["BILI_DOWNLOADS_DIR"], "Show", "E01.mkv")
        video.parent.mkdir(parents=True)
        video.write_bytes(b"video")
        job = web.enqueue_upload([str(video)], "source")
        meta_path = Path(web.JOBS_DIR, job + ".meta.json")
        self.assertEqual(json.loads(meta_path.read_text())["kind"], "upload")
        # Jobs written before the fix had the upload kind clobber the job kind.
        meta_path.write_text(json.dumps({**json.loads(meta_path.read_text()), "kind": "source"}))
        sent = []
        handler = web.H.__new__(web.H)
        handler._send = lambda code, body: sent.append((code, json.loads(body)))
        handler.handle_retry({"job": job})
        code, body = sent[0]
        self.assertEqual(code, 200, body)
        retried = json.loads(Path(web.JOBS_DIR, body["job"] + ".meta.json").read_text())
        self.assertEqual((retried["kind"], retried["upload_kind"], retried["paths"]), ("upload", "source", [str(video)]))


if __name__ == "__main__":
    unittest.main()
