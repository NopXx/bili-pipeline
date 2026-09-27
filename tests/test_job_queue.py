import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from job_queue import JobQueue  # noqa: E402


class FakeProcess:
    next_pid = 1000

    def __init__(self, command, **kwargs):
        self.command = command
        self.returncode = None
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1

    def poll(self):
        return self.returncode


class QueueTests(unittest.TestCase):
    def test_torrent_inspection_starts_during_download(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch(
            "job_queue.subprocess.Popen", FakeProcess
        ):
            queue = JobQueue(directory)
            queue.submit("a" * 16, "download", ["fake", "download"])
            queue.pump()
            queue.submit("b" * 16, "inspect", ["fake", "inspect"])
            queue.pump()
            self.assertEqual(queue.jobs["a" * 16]["status"], "running")
            self.assertEqual(queue.jobs["b" * 16]["status"], "running")

    def test_download_starts_while_conversion_is_running(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch(
            "job_queue.subprocess.Popen", FakeProcess
        ):
            queue = JobQueue(directory)
            queue.submit("c" * 16, "convert", ["fake", "convert"])
            queue.pump()
            self.assertEqual(queue.jobs["c" * 16]["status"], "running")

            queue.submit("d" * 16, "download", ["fake", "download"])
            queue.pump()
            self.assertEqual(queue.jobs["c" * 16]["status"], "running")
            self.assertEqual(queue.jobs["d" * 16]["status"], "running")

    def test_lanes_and_pause_resume(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch("job_queue.subprocess.Popen", FakeProcess), patch(
            "job_queue.os.killpg", create=True
        ) as signal_group, patch("job_queue.signal.SIGSTOP", 19, create=True), patch("job_queue.signal.SIGCONT", 18, create=True):
            completed = []
            queue = JobQueue(directory, lambda item, code: completed.append((item["job"], code)), download_concurrency=1)
            for job, lane in (("a" * 16, "download"), ("b" * 16, "download"),
                              ("c" * 16, "convert"), ("d" * 16, "upload")):
                queue.submit(job, lane, ["fake", job])
            queue.pump()
            self.assertEqual(queue.jobs["a" * 16]["status"], "running")
            self.assertEqual(queue.jobs["b" * 16]["status"], "queued")
            self.assertEqual(queue.jobs["c" * 16]["status"], "running")
            self.assertEqual(queue.jobs["d" * 16]["status"], "running")

            queue.pause("a" * 16)
            queue.pump()
            self.assertEqual(queue.jobs["b" * 16]["status"], "running")
            with self.assertRaisesRegex(ValueError, "busy"):
                queue.resume("a" * 16)
            queue.jobs["b" * 16]["proc"].returncode = 0
            queue.pump()
            queue.resume("a" * 16)
            self.assertEqual(queue.jobs["a" * 16]["status"], "running")
            self.assertEqual(completed, [("b" * 16, 0)])
            self.assertTrue(signal_group.called)

    def test_queued_pause_survives_restart(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            queue = JobQueue(directory)
            queue.submit("e" * 16, "upload", ["fake"])
            queue.pause("e" * 16)
            recovered = JobQueue(directory)
            self.assertEqual(recovered.jobs["e" * 16]["status"], "paused")
            recovered.resume("e" * 16)
            self.assertEqual(recovered.jobs["e" * 16]["status"], "queued")
            state = json.loads((Path(directory) / ("e" * 16 + ".json")).read_text())
            self.assertEqual(state["status"], "queued")

    def test_two_downloads_run_together_and_third_waits(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch(
            "job_queue.subprocess.Popen", FakeProcess
        ):
            queue = JobQueue(directory, download_concurrency=2)
            for letter in "abc":
                queue.submit(letter * 16, "download", ["fake", letter])
            queue.pump()
            self.assertEqual([queue.jobs[letter * 16]["status"] for letter in "abc"],
                             ["running", "running", "queued"])
            queue.jobs["a" * 16]["proc"].returncode = 0
            queue.pump()
            self.assertEqual([queue.jobs[letter * 16]["status"] for letter in "abc"],
                             ["completed", "running", "running"])

    def test_download_concurrency_is_bounded(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            for value in (0, 9, "abc"):
                with self.assertRaisesRegex(ValueError, "download concurrency"):
                    JobQueue(directory, download_concurrency=value)


if __name__ == "__main__":
    unittest.main()
