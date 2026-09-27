#!/usr/bin/env python3
"""
Drive a headless Bili23-Downloader (running under xvfb) through its MCP HTTP
server: parse a Bilibili link, download every episode, wait for them to finish,
and print the absolute path of each finished file — one per line — on stdout.

Bili23 is GUI-only; its download logic lives on the Qt main thread and is only
reachable through the MCP interface the app exposes. So this talks to that
interface rather than importing anything. See scripts/vps-setup.md.

    python3 bili_pull.py <bilibili-url-or-id>

Config (port + token) is read from Bili23's own config.json. Override the file
with BILI_CONFIG, or give the endpoint directly with BILI_MCP_PORT / BILI_MCP_TOKEN.
Quality knobs (all optional env): BILI_VIDEO_QUALITY, BILI_VIDEO_CODEC,
BILI_CONTAINER (default mp4), BILI_LIMIT (max episodes, default 500).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

MODERN_VERSION = "2026-07-28"  # sent as the MCP-Protocol-Version header

TERMINAL_OK = {"completed"}
TERMINAL_FAIL = {"failed", "ffmpeg_failed", "invalid"}


def log(*a):
    stamp = datetime.now().astimezone().strftime("[%Y-%m-%d %H:%M:%S %Z]")
    print(stamp, *a, file=sys.stderr, flush=True)


def fmt_bytes(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return str(value or "0")
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024


def control_tasks(action, task_ids):
    if action not in ("cancel", "pause", "resume"):
        raise ValueError("invalid task action")
    port, token = load_endpoint()
    failed = 0
    for tid in task_ids:
        try:
            call(port, token, f"{action}_task", {"task_id": tid})
            log(f"{action} task {tid}")
        except SystemExit as exc:
            failed += 1
            log(f"could not {action} task {tid}: {exc}")
    return failed


def cancel_tasks(task_ids):
    return control_tasks("cancel", task_ids)


def find_config():
    if p := os.environ.get("BILI_CONFIG"):
        return p
    home = os.path.expanduser("~")
    # Linux QStandardPaths.AppDataLocation, plus the Windows/mac spellings just in case
    candidates = [
        f"{home}/.local/share/Bili23 Downloader/config.json",
        f"{home}/.config/Bili23 Downloader/config.json",
        os.path.join(os.environ.get("APPDATA", ""), "Bili23 Downloader", "config.json"),
        f"{home}/Library/Application Support/Bili23 Downloader/config.json",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def load_endpoint():
    port = os.environ.get("BILI_MCP_PORT")
    token = os.environ.get("BILI_MCP_TOKEN")
    if port and token:
        return int(port), token
    cfg_path = find_config()
    if not cfg_path:
        sys.exit("Bili23 config.json not found — set BILI_CONFIG or BILI_MCP_PORT/BILI_MCP_TOKEN")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    mcp = cfg.get("MCP", {})
    port = int(mcp.get("mcp_port") or 23330)
    token = mcp.get("mcp_token") or ""
    if not token:
        sys.exit(f"No mcp_token in {cfg_path} — enable the MCP server in Bili23 first")
    return port, token


_id = 0


def call(port, token, name, arguments):
    """One tools/call round trip. Returns the tool's structuredContent (a dict)."""
    global _id
    _id += 1
    payload = {
        "jsonrpc": "2.0",
        "id": _id,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments,
        },
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": MODERN_VERSION,  # modern transport requires it as a header
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
    except urllib.error.URLError as e:
        sys.exit(f"MCP request '{name}' failed: {e} — is Bili23 running with MCP enabled?")

    if "error" in body:
        sys.exit(f"MCP '{name}' error: {body['error']}")
    result = body.get("result", {})
    if result.get("isError"):
        text = (result.get("content") or [{}])[0].get("text", "")
        sys.exit(f"Bili23 refused '{name}': {text}")
    # structuredContent when present; else the text block parsed as JSON
    if "structuredContent" in result:
        return result["structuredContent"]
    text = (result.get("content") or [{}])[0].get("text", "{}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def build_options():
    opts = {"container": os.environ.get("BILI_CONTAINER", "mp4")}
    if q := os.environ.get("BILI_VIDEO_QUALITY"):
        opts["video_quality"] = q
    if c := os.environ.get("BILI_VIDEO_CODEC"):
        opts["video_codec"] = c
    return opts


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--cancel":
        raise SystemExit(cancel_tasks(sys.argv[2:]))
    if len(sys.argv) >= 3 and sys.argv[1] in ("--pause", "--resume"):
        raise SystemExit(control_tasks(sys.argv[1][2:], sys.argv[2:]))
    if len(sys.argv) < 2:
        sys.exit("usage: bili_pull.py <bilibili-url-or-id>")
    url = sys.argv[1]
    port, token = load_endpoint()

    login = call(port, token, "get_login_status", {})
    log("login:", login.get("logged_in"), login.get("username") or "")

    limit = int(os.environ.get("BILI_LIMIT", "500"))
    parsed = call(port, token, "parse_url", {"url": url, "limit": limit})
    episodes = parsed.get("episodes", [])

    # parse-only: emit the episode list as JSON for the picker UI, then stop.
    if os.environ.get("BILI_PARSE_ONLY") == "1":
        out = [
            {
                "episode_id": e["episode_id"],
                "title": e.get("title", ""),
                "duration": e.get("duration", ""),
                "needs_reparse": bool(e.get("needs_reparse")),
            }
            for e in episodes
        ]
        print(json.dumps(out, ensure_ascii=False))
        return

    ids = [e["episode_id"] for e in episodes if not e.get("needs_reparse")]
    # BILI_EPISODE_IDS (JSON array) restricts the download to a chosen subset.
    # We still parse everything first — create_download needs the episodes loaded
    # into Bili23's current session.
    if sel := os.environ.get("BILI_EPISODE_IDS"):
        want = set(json.loads(sel))
        ids = [i for i in ids if i in want]
    if not ids:
        sys.exit(f"parse_url returned no downloadable episodes for {url}")
    log(f"parsed {len(ids)} episode(s)")

    created = call(port, token, "create_download", {
        "episode_ids": ids,
        "options": build_options(),
        "redownload": os.environ.get("BILI_REDOWNLOAD") == "1",
    })
    tasks = created.get("tasks", [])
    if not tasks:
        # everything was a duplicate; nothing new to convert
        log("nothing created (all duplicates?) — pass BILI_REDOWNLOAD=1 to force")
        return
    task_ids = [t["task_id"] for t in tasks]
    if state_path := os.environ.get("BILI_JOB_STATE"):
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"task_ids": task_ids}, f)
        os.replace(tmp, state_path)
    log(f"created {len(task_ids)} task(s), waiting…")
    for task in tasks:
        log(f"task {task['task_id']}: {task.get('title') or 'untitled'}")

    done = {}
    previous = {}
    while len(done) < len(task_ids):
        time.sleep(2)
        for tid in task_ids:
            if tid in done:
                continue
            st = call(port, token, "get_task_status", {"task_id": tid})
            status = st.get("status", "")
            if status in TERMINAL_OK:
                path = st.get("file_path")
                if not path or not os.path.exists(path):
                    log(f"task {tid} completed but file missing: {path!r}")
                done[tid] = path
                log(f"✓ {st.get('title','')} -> {path}")
            elif status in TERMINAL_FAIL:
                done[tid] = None
                log(f"✗ task {tid} {status}: {st.get('title','')}")
            else:
                progress = st.get("progress", "")
                downloaded = st.get("downloaded_size")
                total = st.get("total_size")
                speed = st.get("speed")
                snapshot = (status, progress, downloaded, total, speed)
                if snapshot == previous.get(tid):
                    continue
                previous[tid] = snapshot
                details = [f"{progress}%"] if progress != "" else []
                if downloaded is not None or total is not None:
                    details.append(f"{fmt_bytes(downloaded)} / {fmt_bytes(total)}")
                if speed:
                    details.append(f"{fmt_bytes(speed)}/s")
                    try:
                        remaining = max(0, float(total) - float(downloaded))
                        details.append(f"ETA {int(remaining / float(speed))}s")
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass
                log(f"task {tid}: {status} | " + " | ".join(details))

    for path in done.values():
        if path and os.path.exists(path):
            print(path, flush=True)


if __name__ == "__main__":
    main()
