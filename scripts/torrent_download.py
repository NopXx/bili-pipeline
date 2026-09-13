#!/usr/bin/env python3
"""Run one aria2 BitTorrent job and expose progress through the web job state."""

import json
import os
import re
import subprocess
import sys
import time


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".ts", ".mts", ".m2ts"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROGRESS = re.compile(
    r"(?P<done>[^\s/]+)/(?P<total>[^\s(]+)\((?P<pct>\d+)%\)"
)


def write_state(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def downloaded_files(root):
    output = []
    for current, _dirs, names in os.walk(root):
        for name in names:
            if name.endswith(".aria2"):
                continue
            path = os.path.realpath(os.path.join(current, name))
            if path.startswith(os.path.realpath(root) + os.sep) and os.path.isfile(path):
                output.append(path)
    return sorted(output)


def emit(line, state_path, base_state):
    line = ANSI.sub("", line).strip()
    if not line:
        return
    match = PROGRESS.search(line)
    if match:
        details = match.groupdict()
        peers = re.search(r"\bCN:(\d+)", line)
        speed = re.search(r"\bDL:([^\s\]]+)", line)
        eta = re.search(r"\bETA:([^\s\]]+)", line)
        pct = int(details["pct"])
        status = (
            f"torrent | {pct}% | {details['done']} / {details['total']}"
            f" | {speed.group(1) if speed else '0B/s'} | peers {peers.group(1) if peers else '0'}"
        )
        if eta:
            status += f" | ETA {eta.group(1)}"
        print(status, flush=True)
        write_state(state_path, {**base_state, "phase": "torrent", "status": "downloading", "progress": pct})
    elif any(token in line for token in ("NOTICE", "ERROR", "WARN", "Download Results", "FILE:")):
        print(line, flush=True)


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: torrent_download.py SOURCE DESTINATION STATE.json")
    source, destination, state_path = sys.argv[1:]
    os.makedirs(destination, exist_ok=True)
    base_state = {"kind": "torrent", "destination": destination}
    write_state(state_path, {**base_state, "phase": "metadata", "status": "starting", "progress": 0})
    print(f"torrent destination: {destination}", flush=True)
    print("waiting for torrent metadata / peers…", flush=True)

    command = [
        "aria2c",
        "--dir=" + destination,
        "--continue=true",
        "--check-integrity=true",
        "--seed-time=0",
        "--file-allocation=none",
        "--auto-file-renaming=true",
        "--allow-overwrite=false",
        "--follow-torrent=true",
        "--bt-save-metadata=true",
        "--bt-metadata-only=false",
        "--enable-dht=true",
        "--disable-ipv6=true",
        "--enable-peer-exchange=true",
        "--bt-enable-lpd=true",
        "--bt-max-peers=80",
        "--listen-port=6881-6999",
        "--summary-interval=2",
        "--show-console-readout=true",
        "--console-log-level=notice",
        "--enable-color=false",
        source,
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    pending = ""
    assert process.stdout is not None
    while True:
        # aria2 refreshes its console line with CR rather than LF. Reading one
        # character keeps that live progress visible even through a pipe.
        chunk = process.stdout.read(1)
        if not chunk:
            break
        pending += chunk
        parts = re.split(r"[\r\n]+", pending)
        pending = parts.pop()
        for line in parts:
            emit(line, state_path, base_state)
    emit(pending, state_path, base_state)
    code = process.wait()
    files = downloaded_files(destination)
    video_files = [path for path in files if os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS]
    if code == 0 and files:
        write_state(state_path, {
            **base_state,
            "phase": "downloaded",
            "status": "downloaded",
            "progress": 100,
            "files": files,
            "video_files": video_files,
        })
        total = sum(os.path.getsize(path) for path in files)
        print(f"torrent | 100% | complete | {len(files)} files | {total} bytes", flush=True)
        return
    write_state(state_path, {
        **base_state,
        "phase": "failed",
        "status": "failed",
        "progress": 0,
        "exit_code": code,
    })
    raise SystemExit(code or 1)


if __name__ == "__main__":
    main()
