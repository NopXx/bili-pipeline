#!/usr/bin/env python3
"""Run Bili23 Downloader headless (no systemd, no desktop) — e.g. on Kaggle.

    python3 bili23_headless.py install   # clone Bili23 + private venv (idempotent)
    python3 bili23_headless.py start     # seed config.json, start, wait for MCP
    python3 bili23_headless.py restart   # restart, keeping the saved login
    python3 bili23_headless.py status

Bili23 is a Qt GUI app; its downloads are only reachable through the MCP server
it hosts. Qt's `offscreen` platform lets it run without a display or xvfb. It
gets its own venv because it pins packages (protobuf, PySide6) that would clash
with the host's Python. The system needs Qt's runtime libraries (libegl1,
libgl1, libxkbcommon0, libfontconfig1, libdbus-1-3) and ffmpeg.

Environment:
  BILI23_HOME       install/log/pid directory (default /kaggle/tmp/bili23)
  BILI23_REF        Bili23 git tag or branch (default v2.20.0 — the MCP tools
                    bili_pull.py calls were checked against this release)
  BILI_DOWNLOADS_DIR  Bili23 saves into <this>/bilibili so the web UI's file
                    list and upload can see the results
  BILI_CONFIG       Bili23 config.json (default ~/.local/share/Bili23 Downloader)

Use `restart` as BILI23_RESTART_CMD so the web UI's restart button works: it
puts the QR-login cookies back if Bili23 rewrites config.json while quitting.
"""
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time

import bili_pull

REPO = "https://github.com/ScottSloan/Bili23-Downloader.git"
HOME = Path(os.environ.get("BILI23_HOME", "/kaggle/tmp/bili23"))
REF = os.environ.get("BILI23_REF", "v2.20.0")
SOURCE = HOME / "src"
VENV = HOME / "venv"
PYTHON = VENV / "bin" / "python"
PID_FILE = HOME / "bili23.pid"
LOG_FILE = HOME / "bili23.log"
DEFAULT_CONFIG = Path.home() / ".local" / "share" / "Bili23 Downloader" / "config.json"


def config_path():
    return Path(os.environ.get("BILI_CONFIG") or DEFAULT_CONFIG)


def read_config():
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_config(config):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=4), encoding="utf-8")
    os.replace(temporary, path)


def install():
    HOME.mkdir(parents=True, exist_ok=True)
    if not (SOURCE / ".git").is_dir():
        subprocess.run(["git", "clone", "--depth", "1", "--branch", REF, REPO, str(SOURCE)], check=True)
    if not PYTHON.exists():
        if sys.version_info < (3, 11):
            raise SystemExit("Bili23 needs Python 3.11 or newer")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(PYTHON), "-m", "pip", "install", "-q", "-r", str(SOURCE / "requirements.txt")], check=True)
    print(f"Bili23 {REF} installed in {HOME}")


def seed_config():
    """Enable MCP and point downloads at BILI_DOWNLOADS_DIR, keeping everything else."""
    config = read_config()
    mcp = config.setdefault("MCP", {})
    mcp["mcp_enabled"] = True
    mcp.setdefault("mcp_port", 23330)
    if not mcp.get("mcp_token"):
        mcp["mcp_token"] = secrets.token_urlsafe(32)
    downloads = os.environ.get("BILI_DOWNLOADS_DIR")
    if downloads:
        target = Path(downloads) / "bilibili"
        target.mkdir(parents=True, exist_ok=True)
        config.setdefault("Download", {})["download_path"] = str(target)
    # A config without a version is treated as old and migrated, which resets
    # naming rules and queues a notice dialog. Stamp the current version.
    match = re.search(r"app_config_version\s*=\s*(\d+)", (SOURCE / "src/util/common/config.py").read_text(encoding="utf-8"))
    if match:
        config.setdefault("Application", {}).setdefault("config_version", int(match.group(1)))
    write_config(config)


def running_pid():
    try:
        pid = int(PID_FILE.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def mcp_ready():
    try:
        port, token = bili_pull.load_endpoint()
        return bili_pull.call(port, token, "get_login_status", {})
    except SystemExit:
        return None


def start(wait=90):
    if not PYTHON.exists():
        raise SystemExit("Bili23 is not installed; run `install` first")
    if running_pid():
        print("Bili23 already running")
        return
    seed_config()
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONUNBUFFERED": "1"}
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(PYTHON), "src/main.py"], cwd=SOURCE, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    PID_FILE.write_text(str(process.pid))
    deadline = time.time() + wait
    while time.time() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"Bili23 exited with code {process.returncode}; see {LOG_FILE}:\n" + LOG_FILE.read_text(errors="replace")[-1500:])
        login = mcp_ready()
        if login is not None:
            print(f"Bili23 ready (pid {process.pid}); logged in: {bool(login.get('logged_in'))} {login.get('username') or ''}".rstrip())
            return
        time.sleep(2)
    raise SystemExit(f"Bili23 MCP did not answer within {wait}s; see {LOG_FILE}")


def stop(timeout=20):
    pid = running_pid()
    if not pid:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                PID_FILE.unlink(missing_ok=True)
                return
            time.sleep(0.5)
    raise SystemExit(f"Bili23 (pid {pid}) did not stop")


def restart():
    # The web UI writes the QR-login cookies straight into config.json while
    # Bili23 runs; Bili23 may save its older in-memory copy as it quits.
    cookie = read_config().get("Cookie") or {}
    stop()
    if cookie.get("SESSDATA"):
        config = read_config()
        config["Cookie"] = {**(config.get("Cookie") or {}), **cookie}
        write_config(config)
    start()


def status():
    login = mcp_ready()
    print(json.dumps({"pid": running_pid(), "mcp": login is not None, "login": login}, ensure_ascii=False))


if __name__ == "__main__":
    commands = {"install": install, "start": start, "restart": restart, "stop": stop, "status": status}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit(f"usage: {sys.argv[0]} {'|'.join(commands)}")
    commands[sys.argv[1]]()
