#!/usr/bin/env python3
"""Run one unshackle (DRM downloader) job for the web UI and mirror its progress.

Usage: unshackle_pull.py CONFIG_JSON STATE_JSON

CONFIG_JSON holds the unshackle /api/download body (service, title_id, quality, range, a_lang, s_lang, ...)
plus "destination": unshackle's own downloads directory, which the notebook points inside the web UI's
downloads dir so the file shows in the Files tab. (unshackle 6.0 rejects an output_dir outside its
downloads directory, so the folder is set in its config, not per job.)
unshackle serve must run on UNSHACKLE_URL (default http://127.0.0.1:8786) with key UNSHACKLE_KEY.
SIGTERM (the web UI's cancel) cancels the unshackle job too.
"""

import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("UNSHACKLE_URL", "http://127.0.0.1:8786").rstrip("/")
KEY = os.environ.get("UNSHACKLE_KEY", "")
POLL_SECONDS = float(os.environ.get("UNSHACKLE_POLL_SECONDS", "3"))
ACTIVE = ("queued", "downloading")


def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)


def call(path, body=None, method=None):
    request = urllib.request.Request(
        BASE + path, data=None if body is None else json.dumps(body).encode(), method=method,
        headers={"Content-Type": "application/json", "X-Secret-Key": KEY},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("message") or detail
        except ValueError:
            pass
        raise RuntimeError(f"unshackle {exc.code}: {detail[:400]}") from None
    return json.loads(raw) if raw else {}


def clean_stderr(text):
    """Readable lines from unshackle's stderr: drop ANSI codes, rich box borders and blanks."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    lines = (line.strip(" │╭╮╰╯─┃") .strip() for line in text.splitlines())
    return [line for line in lines if line and not set(line) <= set("─━═ ")]


def update_state(state_path, **values):
    """Merge values into the job's state file (the queue owns "status"; never set it here)."""
    try:
        with open(state_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    data.update(values)
    temporary = state_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(temporary, state_path)


def main(config_path, state_path):
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    destination = config.pop("destination")
    body = config

    job = call("/api/download", body)["job_id"]
    log(f"unshackle job {job}: {body['service']} {body['title_id']}")
    update_state(state_path, unshackle_job=job, phase="queued", destination=destination)

    def cancel(signum, frame):
        del signum, frame
        try:
            call(f"/api/download/jobs/{job}", method="DELETE")
            log("cancelled the unshackle job")
        except Exception as exc:  # noqa: BLE001 - best effort; we are exiting anyway
            log(f"cancel failed: {exc}")
        sys.exit(1)

    signal.signal(signal.SIGTERM, cancel)

    last_phase = None
    while True:
        try:
            data = call(f"/api/download/jobs/{job}")
        except (RuntimeError, OSError) as exc:  # a dropped poll must not kill a long download
            log(f"poll failed, retrying: {exc}")
            time.sleep(POLL_SECONDS * 3)
            continue
        status, phase = data.get("status"), data.get("phase") or data.get("status")
        if phase != last_phase:
            log(f"{status}: {phase}")
            last_phase = phase
        update_state(
            state_path,
            progress=round(float(data.get("progress") or 0), 1),
            phase=phase,
            speed=data.get("speed"),
            eta=data.get("eta"),
            title=data.get("title") or body["title_id"],
            done=data.get("completed_tracks"),
            total=data.get("total_tracks"),
        )
        if status not in ACTIVE:
            break
        time.sleep(POLL_SECONDS)

    if status != "completed":
        # error_message is generic ("Download failed with exit code 1"); the cause is in the
        # worker's stderr (e.g. "There's no 432p Video Track..."). Log its tail, surface the last line.
        stderr = clean_stderr(data.get("worker_stderr") or "")
        for line in stderr[-15:]:
            log(f"  unshackle: {line}")
        error = (stderr[-1] if stderr else "") or data.get("error_details") or data.get("error_message") or status
        update_state(state_path, error=str(error)[:500])
        log(f"failed: {error}")
        return 1
    # unshackle reports paths relative to its own working dir; find them by name under the destination.
    names = {os.path.basename(path) for path in data.get("output_files") or []}
    files = sorted(
        os.path.join(root, name)
        for root, _, found in os.walk(destination, followlinks=True)
        for name in found
        if name in names
    )
    update_state(state_path, files=files, progress=100)
    log("done: " + ", ".join(files))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
