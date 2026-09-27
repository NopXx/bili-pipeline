"""Small persistent, per-lane subprocess queue for the Bili Studio web server."""

import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


LANES = ("inspect", "download", "convert", "upload")
ACTIVE = {"queued", "running", "paused"}


def visible_convert_gpus():
    """Return CUDA device selectors the server is permitted to use."""
    configured = os.environ.get("BILI_CONVERT_GPUS")
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if configured is not None:
        return [gpu.strip() for gpu in configured.split(",") if gpu.strip()][:2]
    if visible is not None:
        return [gpu.strip() for gpu in visible.split(",") if gpu.strip() and gpu.strip() != "-1"][:2]
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()][:2]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return []


class JobQueue:
    def __init__(self, jobs_dir, on_complete=None, download_concurrency=None, convert_gpus=None):
        if download_concurrency is None:
            download_concurrency = os.environ.get("BILI_DOWNLOAD_CONCURRENCY", "2")
        try:
            download_concurrency = int(download_concurrency)
        except (TypeError, ValueError) as exc:
            raise ValueError("download concurrency must be an integer from 1 to 8") from exc
        if not 1 <= download_concurrency <= 8:
            raise ValueError("download concurrency must be an integer from 1 to 8")
        self.limits = {lane: 1 for lane in LANES}
        self.limits["download"] = download_concurrency
        self.convert_gpus = visible_convert_gpus() if convert_gpus is None else [str(gpu) for gpu in convert_gpus][:2]
        self.limits["convert"] = max(1, len(self.convert_gpus))
        self.root = Path(jobs_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.on_complete = on_complete
        self.jobs = {}
        self.lock = threading.RLock()
        self._recover()

    def _state(self, item, **changes):
        path = Path(item["state"])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.update(changes)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _persist(self, item):
        record = {key: item[key] for key in ("job", "lane", "command", "env_overrides", "log", "state", "meta", "status", "created_at", "gpu")}
        record["pid"] = item["proc"].pid if item["proc"] is not None else item.get("pid")
        path = self.root / f"{item['job']}.queue.json"
        temporary = Path(str(path) + ".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _recover(self):
        for path in self.root.glob("*.queue.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("lane") not in LANES or not record.get("command"):
                    continue
                record["proc"] = None
                record.setdefault("gpu", None)
                record["cancelled"] = False
                # Never mistake an orphaned worker for a managed process after
                # a web-server restart. Queued work survives; running work is
                # marked interrupted for explicit retry.
                if record["status"] == "running":
                    self._stop_orphan(record)
                    record["status"] = "interrupted"
                    self._state(record, status="interrupted", phase="interrupted", error="web server restarted; retry this job")
                    self._persist(record)
                elif record["status"] == "paused" and record.get("pid"):
                    self._stop_orphan(record)
                    record["status"] = "interrupted"
                    self._state(record, status="interrupted", phase="interrupted", error="web server restarted while paused; retry this job")
                    self._persist(record)
                self.jobs[record["job"]] = record
            except (OSError, ValueError, KeyError):
                continue

    @staticmethod
    def _stop_orphan(record):
        pid = record.get("pid")
        if not isinstance(pid, int) or pid < 2 or not hasattr(os, "killpg"):
            return
        try:
            environment = Path(f"/proc/{pid}/environ").read_bytes()
            marker = f"BILI_QUEUE_JOB_ID={record['job']}".encode() + b"\0"
            if marker not in environment:
                return
            os.killpg(pid, signal.SIGCONT)
            os.killpg(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    def submit(self, job, lane, command, env_overrides=None):
        if lane not in LANES:
            raise ValueError(f"invalid queue lane: {lane}")
        if not isinstance(command, list) or not command:
            raise ValueError("command required")
        with self.lock:
            if job in self.jobs:
                raise ValueError("duplicate job")
            item = {
                "job": job, "lane": lane, "command": command,
                "env_overrides": env_overrides or {},
                "log": str(self.root / f"{job}.log"),
                "state": str(self.root / f"{job}.json"),
                "meta": str(self.root / f"{job}.meta.json"),
                "status": "queued", "created_at": time.time(),
                "proc": None, "cancelled": False, "gpu": None,
            }
            Path(item["log"]).touch()
            self._state(item, status="queued", phase="queued")
            self.jobs[job] = item
            self._persist(item)
            return item

    def _signal(self, item, sig):
        proc = item["proc"]
        if proc is None or proc.poll() is not None:
            raise RuntimeError("job process is no longer running")
        if not hasattr(os, "killpg"):
            raise RuntimeError("process pause requires a Unix host")
        os.killpg(proc.pid, sig)

    def pause(self, job, before_pause=None):
        with self.lock:
            item = self.jobs[job]
            if item["lane"] not in ("download", "upload"):
                raise ValueError("only downloads and uploads can be paused")
            if item["status"] == "queued":
                item["status"] = "paused"
            elif item["status"] == "running":
                if before_pause:
                    before_pause(item)
                self._signal(item, signal.SIGSTOP)
                item["status"] = "paused"
            else:
                raise ValueError("job is not running or queued")
            self._state(item, status="paused", phase="paused")
            self._persist(item)
            return item

    def resume(self, job, before_resume=None):
        with self.lock:
            item = self.jobs[job]
            if item["status"] != "paused":
                raise ValueError("job is not paused")
            if item["proc"] is None:
                item["status"] = "queued"
            else:
                if sum(other is not item and other["lane"] == item["lane"] and other["status"] == "running"
                       for other in self.jobs.values()) >= self.limits[item["lane"]]:
                    raise ValueError("queue lane is busy; resume after the current job finishes")
                if before_resume:
                    before_resume(item)
                self._signal(item, signal.SIGCONT)
                item["status"] = "running"
            self._state(item, status=item["status"], phase=item["lane"])
            self._persist(item)
            return item

    def cancel(self, job, before_cancel=None):
        with self.lock:
            item = self.jobs[job]
            if item["status"] not in ACTIVE:
                raise ValueError("job is not active")
            if before_cancel and item["proc"] is not None:
                before_cancel(item)
            proc = item["proc"]
            if proc is not None and proc.poll() is None:
                if item["status"] == "paused":
                    self._signal(item, signal.SIGCONT)
                os.killpg(proc.pid, signal.SIGTERM)
            item["status"] = "cancelled"
            item["cancelled"] = True
            self._state(item, status="cancelled", phase="cancelled")
            self._persist(item)
            with open(item["log"], "a", encoding="utf-8") as out:
                out.write("\n==> CANCELLED BY USER\n")
            return item

    def pump(self):
        completed = []
        with self.lock:
            for item in self.jobs.values():
                proc = item["proc"]
                if item["status"] != "running" or proc is None:
                    continue
                code = proc.poll()
                if code is None:
                    continue
                item["status"] = "completed" if code == 0 else "failed"
                if code != 0:
                    self._state(item, status="failed", phase="failed", exit_code=code)
                self._persist(item)
                completed.append((item, code))
            for lane in LANES:
                active = sum(item["lane"] == lane and item["status"] == "running" for item in self.jobs.values())
                slots = self.limits[lane] - active
                if slots <= 0:
                    continue
                pending = sorted(
                    (item for item in self.jobs.values() if item["lane"] == lane and item["status"] == "queued"),
                    key=lambda item: item["created_at"],
                )
                for item in pending:
                    if slots <= 0:
                        break
                    try:
                        gpu = None
                        if lane == "convert" and self.convert_gpus:
                            busy = {other["gpu"] for other in self.jobs.values()
                                    if other["lane"] == "convert" and other["status"] == "running"}
                            gpu = next(device for device in self.convert_gpus if device not in busy)
                        worker_env = {**os.environ, **item["env_overrides"], "BILI_QUEUE_JOB_ID": item["job"]}
                        if gpu is not None:
                            worker_env["CUDA_VISIBLE_DEVICES"] = gpu
                        log = open(item["log"], "a", encoding="utf-8")
                        try:
                            proc = subprocess.Popen(
                                item["command"], env=worker_env,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                            )
                        finally:
                            log.close()
                        item["proc"] = proc
                        item["gpu"] = gpu
                        item["status"] = "running"
                        self._state(item, status="running", phase=lane, gpu=gpu)
                        self._persist(item)
                        slots -= 1
                    except Exception as exc:
                        item["status"] = "failed"
                        self._state(item, status="failed", phase="failed", error=str(exc))
                        self._persist(item)
        for item, code in completed:
            if self.on_complete:
                self.on_complete(item, code)

    def run(self):
        while True:
            try:
                self.pump()
            except Exception as exc:
                print(f"queue scheduler error: {exc}", flush=True)
            time.sleep(0.5)
