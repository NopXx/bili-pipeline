#!/usr/bin/env python3
"""Transfer finished HLS bundles or original downloads in the upload lane."""

import json
import os
from pathlib import Path
import shutil
import sys

from process_media import DOWNLOADS, HLS_ROOT, RCLONE_REMOTE, ROOT, run_with_progress, update_state


def contained(path, root):
    resolved = Path(path).resolve()
    return resolved if root in resolved.parents else None


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: upload_media.py config.json state.json")
    config_path, state_path = map(Path, sys.argv[1:])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    kind = config.get("kind")
    if kind not in ("hls", "source"):
        raise ValueError("upload kind must be hls or source")
    paths = [Path(item).resolve() for item in config.get("paths", [])]
    if not paths:
        raise ValueError("no upload paths")
    root = HLS_ROOT if kind == "hls" else DOWNLOADS
    for path in paths:
        if not contained(path, root) or not (path.is_dir() if kind == "hls" else path.is_file()):
            raise ValueError(f"invalid upload path: {path}")

    env = dict(os.environ)
    update_state(state_path, phase="upload", status="running", progress=0)
    try:
        for index, path in enumerate(paths, 1):
            print(f"==> uploading [{index}/{len(paths)}]: {path.name}", flush=True)
            update_state(state_path, current_file=str(path), progress=0, speed="", done="", total="", eta="",
                         file_index=index, file_count=len(paths))
            if RCLONE_REMOTE:
                destination = f"{RCLONE_REMOTE}/{path.name}" if kind == "hls" else f"{RCLONE_REMOTE}/{path.stem}/{path.name}"
                command = ["rclone", "copy" if kind == "hls" else "copyto", str(path), destination,
                           "--stats=5s", "--stats-one-line", "--stats-log-level", "NOTICE"]
            else:
                folder_id = env.get("DRIVE_FOLDER_ID")
                if not folder_id:
                    raise RuntimeError("DRIVE_FOLDER_ID is not configured")
                script = ROOT / "scripts" / ("drive_push.mjs" if kind == "hls" else "drive_upload.mjs")
                command = ["node", str(script), str(path), folder_id] if kind == "hls" else ["node", str(script), folder_id, str(path)]
            run_with_progress(command, env, 0, "UPLOAD", state_path)
            update_state(state_path, progress=int(index * 100 / len(paths)))
            if kind == "source" and config.get("remove_source"):
                # Only for auto-uploaded torrents; rclone/Drive already
                # confirmed this file, and later files may still need space.
                path.unlink()
                print(f"removed uploaded source: {path}", flush=True)

        # Keep source downloads unless the job asked for remove_source. Remove job-owned HLS bundles
        # after every upload in this job has succeeded.
        if kind == "hls" and not config.get("keep_local", False):
            for path in paths:
                if not contained(path, HLS_ROOT) or len(path.relative_to(HLS_ROOT).parts) < 2:
                    raise RuntimeError(f"refusing unsafe HLS cleanup: {path}")
                shutil.rmtree(path)
                print(f"removed uploaded HLS bundle: {path}", flush=True)
        update_state(state_path, phase="completed", status="completed", progress=100)
        print("==> upload completed", flush=True)
    except Exception as exc:
        update_state(state_path, phase="failed", status="failed", error=str(exc))
        raise


if __name__ == "__main__":
    main()
