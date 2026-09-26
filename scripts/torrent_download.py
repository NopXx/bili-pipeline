#!/usr/bin/env python3
"""Run one aria2 BitTorrent job and expose progress through the web job state."""

import json
import os
import re
import subprocess
import sys
import time
import tempfile
import urllib.request


def torrent_files(payload):
    pos = 0
    def read(depth=0):
        nonlocal pos
        if depth > 64 or pos >= len(payload):
            raise ValueError("invalid torrent metadata")
        tag = payload[pos:pos + 1]
        if tag == b"i":
            end = payload.index(b"e", pos)
            value = int(payload[pos + 1:end]); pos = end + 1
            return value
        if tag in (b"l", b"d"):
            pos += 1
            value = [] if tag == b"l" else {}
            while payload[pos:pos + 1] != b"e":
                item = read(depth + 1)
                if tag == b"l": value.append(item)
                else: value[item] = read(depth + 1)
            pos += 1
            return value
        end = payload.index(b":", pos)
        size = int(payload[pos:end]); pos = end + 1
        if size < 0 or pos + size > len(payload): raise ValueError("invalid string")
        value = payload[pos:pos + size]; pos += size
        return value
    metadata = read()
    if pos != len(payload): raise ValueError("trailing torrent data")
    info = metadata[b"info"]
    def component(value):
        value = value.decode("utf-8", "replace")
        if value in ("", ".", "..") or any(c in value for c in ("/", "\\", "\0")):
            raise ValueError("unsafe torrent path")
        return value
    name = component(info.get(b"name.utf-8", info[b"name"]))
    entries = info.get(b"files")
    files = []
    for index, entry in enumerate(entries if entries is not None else [info], 1):
        parts = entry.get(b"path.utf-8", entry.get(b"path", [])) if entries is not None else []
        path = "/".join([name] + [component(part) for part in parts])
        size = entry[b"length"]
        if type(size) is not int or size < 0: raise ValueError("invalid file size")
        files.append({"index": index, "path": path, "size": size})
    if not files: raise ValueError("torrent contains no files")
    return name, files


def inspect_torrent(source, state_path):
    write_state(state_path, {"kind": "torrent_inspect", "phase": "metadata", "status": "starting"})
    prepared = state_path + ".prepared.torrent"
    try:
        if source.startswith("magnet:?"):
            with tempfile.TemporaryDirectory(prefix="bili-torrent-metadata-") as folder:
                subprocess.run(["aria2c", "--dir=" + folder, "--bt-metadata-only=true",
                                "--bt-save-metadata=true", "--seed-time=0", "--stop=120",
                                "--enable-dht=true", "--enable-peer-exchange=true", source], check=True)
                paths = [os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".torrent")]
                if len(paths) != 1: raise ValueError("metadata not found; try again when peers are available")
                with open(paths[0], "rb") as handle: payload = handle.read(4 * 1024 * 1024 + 1)
        elif source.startswith(("http://", "https://")):
            with urllib.request.urlopen(source, timeout=30) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
        else:
            with open(source, "rb") as handle: payload = handle.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024: raise ValueError("torrent exceeds 4 MB")
        name, files = torrent_files(payload)
        with open(prepared, "wb") as handle: handle.write(payload)
        write_state(state_path, {"kind": "torrent_inspect", "phase": "ready", "status": "completed",
                                "name": name, "torrent_files": files})
        print("Torrent file list ready", flush=True)
    except Exception as error:
        write_state(state_path, {"kind": "torrent_inspect", "phase": "failed", "status": "failed", "error": str(error)})
        raise


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


def emit(line, state_path, base_state, log_state):
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
        # aria2 emits the same percentage (and FILE path) every few seconds.
        # Keep the web log readable while its progress bar tracks each change.
        if pct != log_state["progress"]:
            print(status, flush=True)
            write_state(state_path, {**base_state, "phase": "torrent", "status": "downloading", "progress": pct})
            log_state["progress"] = pct
    elif line.startswith("FILE:"):
        if line not in log_state["files"]:
            print(line, flush=True)
            log_state["files"].add(line)
    elif any(token in line for token in ("NOTICE", "ERROR", "WARN", "Download Results")):
        print(line, flush=True)


def main():
    if len(sys.argv) == 5 and sys.argv[1] == "--inspect":
        inspect_torrent(sys.argv[2], sys.argv[4])
        return
    if len(sys.argv) not in (4, 5):
        raise SystemExit("usage: torrent_download.py SOURCE DESTINATION STATE.json")
    source, destination, state_path = sys.argv[1:4]
    selected = [int(n) for n in sys.argv[4].split(",")] if len(sys.argv) == 5 else []
    allowed_paths = None
    if selected:
        with open(source, "rb") as handle: _, entries = torrent_files(handle.read())
        allowed_paths = {os.path.realpath(os.path.join(destination, e["path"])) for e in entries if e["index"] in selected}
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
        command + (["--select-file=" + ",".join(map(str, selected)), "--bt-remove-unselected-file=true"] if selected else []),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    pending = ""
    log_state = {"progress": None, "files": set()}
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
            emit(line, state_path, base_state, log_state)
    emit(pending, state_path, base_state, log_state)
    code = process.wait()
    files = downloaded_files(destination)
    if allowed_paths is not None:
        files = [path for path in files if path in allowed_paths]
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
