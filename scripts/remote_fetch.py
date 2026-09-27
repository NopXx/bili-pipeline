#!/usr/bin/env python3
"""Download one file from the rclone remote and prove it is intact.

    remote_fetch.py CONFIG.json STATE.json

CONFIG holds {"source": "<remote>:<path>", "destination": "<local dir>"}.
A corrupt object on Drive can be many gigabytes, so an MKV/WebM's EBML header
is checked with a 16-byte read before anything is downloaded. After the copy
the local file must be non-empty, keep that header, and parse with ffprobe;
if not, the copy is retried once with multi-threaded transfer turned off.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from process_media import update_state

EBML = b"\x1aE\xdf\xa3"
STATS = re.compile(
    r"([\d.]+\s*[kKMGTPE]?i?B)\s*/\s*([\d.]+\s*[kKMGTPE]?i?B),\s*(\d{1,3})%,\s*([\d.]+\s*[kKMGTPE]?i?B/s)(?:,\s*ETA\s+([^\s,]+))?"
)


def hexbytes(data):
    return " ".join(f"{b:02X}" for b in data)


def check_remote_header(source, ext):
    result = subprocess.run(["rclone", "cat", source, "--head", "16"], capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError("cannot read the remote file: " + result.stderr.decode(errors="replace").strip()[-400:])
    print("remote header:", hexbytes(result.stdout), flush=True)
    if ext in (".mkv", ".webm") and not result.stdout.startswith(EBML):
        raise RuntimeError(f"the remote file is not a valid MKV/WebM (expected 1A 45 DF A3, got {hexbytes(result.stdout[:8])}); not downloading")


def copy(source, local, state_path, extra):
    command = ["rclone", "copyto", source, str(local), "--retries", "3", "--low-level-retries", "10",
               "--stats=2s", "--stats-one-line", "--stats-log-level", "NOTICE", *extra]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
    last = None
    assert process.stdout is not None
    for line in process.stdout:
        stats = STATS.search(line)
        if not stats:
            print(line.rstrip(), flush=True)
            continue
        done, total, percent, speed, eta = stats.groups()
        percent = min(100, int(percent))
        update_state(state_path, progress=percent, done=done, total=total, speed=speed, eta=eta if eta and eta != "-" else "")
        if percent != last:
            print(f"remote | {percent}% | {speed} · {done} / {total}", flush=True)
            last = percent
    if process.wait():
        raise RuntimeError(f"rclone copyto failed with exit code {process.returncode}")


def verify(local, ext):
    if not local.is_file() or local.stat().st_size == 0:
        raise RuntimeError(f"downloaded file is missing or empty: {local}")
    with local.open("rb") as handle:
        head = handle.read(16)
    if ext in (".mkv", ".webm") and not head.startswith(EBML):
        raise RuntimeError(f"downloaded MKV/WebM has an invalid header: {hexbytes(head[:8])}")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name,duration", "-of", "json", str(local)],
                           capture_output=True, text=True, timeout=300)
    if probe.returncode:
        raise RuntimeError("ffprobe cannot read the downloaded file: " + probe.stderr.strip()[-400:])
    info = json.loads(probe.stdout or "{}").get("format", {})
    print(f"verified: {info.get('format_name', '?')}, {float(info.get('duration') or 0):.0f}s, {local.stat().st_size} bytes", flush=True)


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: remote_fetch.py CONFIG.json STATE.json")
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    state_path = sys.argv[2]
    source = config["source"]
    destination = Path(config["destination"])
    destination.mkdir(parents=True, exist_ok=True)
    local = destination / source.rsplit("/", 1)[-1].split(":", 1)[-1]
    ext = local.suffix.lower()
    update_state(state_path, kind="remote_download", phase="remote", status="downloading", progress=0, destination=str(local))
    try:
        print(f"remote source: {source}", flush=True)
        check_remote_header(source, ext)
        attempts = (("normal", []), ("single-stream retry", ["--multi-thread-streams", "0"]))
        for number, (label, extra) in enumerate(attempts, 1):
            local.unlink(missing_ok=True)
            print(f"==> download attempt {number}/{len(attempts)}: {label}", flush=True)
            try:
                copy(source, local, state_path, extra)
                verify(local, ext)
                break
            except Exception as exc:
                print(f"attempt failed: {exc}", flush=True)
                if number == len(attempts):
                    raise
        update_state(state_path, phase="downloaded", status="downloaded", progress=100,
                     files=[str(local)], video_files=[str(local)])
        print(f"==> downloaded: {local}", flush=True)
    except Exception as exc:
        update_state(state_path, phase="failed", status="failed", error=str(exc))
        local.unlink(missing_ok=True)
        try:
            destination.rmdir()
        except OSError:
            pass
        raise


if __name__ == "__main__":
    main()
