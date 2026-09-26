#!/usr/bin/env python3
"""Validate downloaded media, build HLS with the selected profile, then upload."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
PREP = ROOT / "public" / "prep-hls.sh"
PUSH = ROOT / "scripts" / "drive_push.mjs"
DRIVE_UPLOAD = ROOT / "scripts" / "drive_upload.mjs"
RCLONE_REMOTE = os.environ.get("BILI_RCLONE_REMOTE", "").rstrip("/")
DOWNLOADS = Path(os.environ.get("BILI_DOWNLOADS_DIR", "/opt/bili-downloads")).resolve()
HLS_ROOT = Path(os.environ.get("BILI_HLS_DIR", "/opt/bili-hls")).resolve()


def update_state(path, **values):
    data = {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    data.update(values)
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def probe(path):
    raw = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ], text=True)
    return json.loads(raw)


def duration(stream):
    try:
        return float(stream.get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def validate(path):
    info = probe(path)
    video = next((x for x in info.get("streams", []) if x.get("codec_type") == "video"), None)
    audio = next((x for x in info.get("streams", []) if x.get("codec_type") == "audio"), None)
    if not video:
        raise RuntimeError(f"no video stream: {path.name}")
    vd = duration(video)
    ad = duration(audio) if audio else vd
    total = duration(info.get("format", {}))
    if not vd:
        vd = total
    if audio and vd and ad and abs(vd - ad) > max(5.0, total * 0.01):
        raise RuntimeError(
            f"media validation failed: video {vd:.1f}s but audio {ad:.1f}s; "
            "the download is incomplete or corrupt"
        )
    print(f"validated: {path.name} | {video.get('codec_name')} {video.get('width')}x{video.get('height')} | {total:.1f}s", flush=True)
    return total


def _human_speed(bytes_per_sec):
    try:
        bps = float(bytes_per_sec)
    except (TypeError, ValueError):
        return ""
    if bps >= 1e6:
        return f"{bps / 1e6:.1f} MB/s"
    if bps >= 1e3:
        return f"{bps / 1e3:.0f} KB/s"
    return f"{int(bps)} B/s"


def run_with_progress(command, env, total, label, state_path):
    proc = subprocess.Popen(
        command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", bufsize=1,
    )
    last_percent = -1
    upload_total = 0
    upload_done = 0
    assert proc.stdout is not None
    for line in proc.stdout:
        text = line.rstrip()
        # A child can stream a byte-level percent with an optional bytes/sec speed
        # (drive_upload.mjs does this every 2s so a multi-GB upload's bar and speed
        # move instead of sitting idle). It already throttles, so each marker is
        # emitted as-is (not deduped) and the raw marker line is not echoed.
        pct_marker = re.match(r"UPLOAD_PCT\s+(\d+)(?:\s+(\d+))?", text)
        if pct_marker:
            percent = min(100, int(pct_marker.group(1)))
            speed = f" {_human_speed(pct_marker.group(2))}" if pct_marker.group(2) else ""
            print(f"{label} | {percent}% |{speed}", flush=True)
            update_state(state_path, progress=percent)
            last_percent = percent
            continue
        # rclone --stats-one-line prints e.g.
        # `208.382 MiB / 8.501 GiB, 2%, 32.095 MiB/s, ETA 4m24s`.
        # Its NOTICE prefix varies by version, so match the transfer summary.
        rclone_progress = None
        if label == "UPLOAD":
            rclone_progress = re.search(
                r"([\d.]+\s+[kMGTPE]?i?B)\s*/\s*"
                r"([\d.]+\s+[kMGTPE]?i?B),\s*"
                r"(\d{1,3})%,\s*"
                r"([\d.]+\s+[kMGTPE]?i?B/s)",
                text,
            )
        if rclone_progress:
            percent = min(100, int(rclone_progress.group(3)))
            if percent != last_percent:
                print(
                    f"UPLOAD | {percent}% | {rclone_progress.group(4)} "
                    f"· {rclone_progress.group(1)} / {rclone_progress.group(2)}",
                    flush=True,
                )
                update_state(state_path, progress=percent)
                last_percent = percent
            continue
        # hls-prep reports `[bar] 12.34% 00:14:54 / 02:00:51 300 fps 12x`.
        # It does not emit ffmpeg's `time=`, and its terminal bar can include
        # control/ANSI sequences. Keep only compact, whole-percent milestones
        # in the job log while mirroring the value to the state file.
        hls_progress = None
        if label == "HLS":
            clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
            hls_progress = re.search(
                r"(?<!\d)(\d{1,3}(?:\.\d+)?)%\s+"
                r"(\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)\s*/\s*"
                r"(\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)",
                clean,
            )
        if hls_progress:
            percent = min(100, int(float(hls_progress.group(1))))
            if percent != last_percent:
                print(
                    f"HLS | {percent}% | {hls_progress.group(2)} / {hls_progress.group(3)}",
                    flush=True,
                )
                update_state(state_path, progress=percent)
                last_percent = percent
            continue
        print(text, flush=True)
        percent = None
        # Upload with no byte stream falls back to a count: drive_push announces
        # the file count up front and we advance one step per uploaded piece.
        marker = re.match(r"\s*to upload:\s*(\d+)", text)
        if marker:
            upload_total = int(marker.group(1))
        elif upload_total and text.lstrip().startswith("uploaded "):
            upload_done += 1
            percent = min(100, int(upload_done * 100 / upload_total))
        elif total:
            match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", text)
            if match:
                elapsed = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
                percent = min(100, int(elapsed * 100 / total))
        if percent is not None and percent != last_percent:
            # Trailing pipe keeps the web UI's `| N% |` progress regex matching,
            # and the mirrored state value survives even when the live log tail
            # scrolls past the last percent line during a long, slow encode.
            print(f"{label} | {percent}% |", flush=True)
            update_state(state_path, progress=percent)
            last_percent = percent
    code = proc.wait()
    if code:
        raise subprocess.CalledProcessError(code, command)


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: process_media.py config.json state.json")
    config_path, state_path = map(Path, sys.argv[1:])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    files = [Path(item).resolve() for item in config.get("files", [])]
    if not files:
        raise RuntimeError("no downloaded files selected")
    for path in files:
        if DOWNLOADS not in path.parents or not path.is_file():
            raise RuntimeError(f"invalid downloaded file: {path}")

    env = dict(os.environ)
    mappings = {
        "copy_video": "PREP_COPY_VIDEO", "reencode": "PREP_REENCODE", "ladder": "PREP_LADDER",
        "auto_hdr": "PREP_AUTO_HDR", "preserve_hdr": "PREP_PRESERVE_HDR",
        "gpu_tonemap": "PREP_GPU_TONEMAP", "copy_audio": "PREP_COPY_AUDIO",
        "ladder_heights": "PREP_LADDER_HEIGHTS", "ladder_bitrates": "PREP_LADDER_BITRATES",
        "height": "PREP_HEIGHT", "video_bitrate": "PREP_VIDEO_BITRATE",
        "audio_bitrate": "PREP_AUDIO_BITRATE", "audio_channels": "PREP_AUDIO_CHANNELS",
        "segment_seconds": "PREP_SEGMENT_SECONDS", "poster_seconds": "PREP_POSTER_SECONDS",
    }
    for key, target in mappings.items():
        value = config.get(key)
        if value not in (None, "", False, "auto"):
            env[target] = "1" if value is True else str(value)

    # Upload the original files straight to Drive, no HLS conversion. Each file
    # lands in its own sub-folder (drive_upload.mjs), and the per-file "uploaded"
    # lines drive the same progress bar as an HLS upload.
    if config.get("upload_source"):
        update_state(state_path, phase="upload", status="running", progress=0, files=[str(x) for x in files])
        try:
            print(f"==> uploading: {len(files)} source file(s) to Drive", flush=True)
            if RCLONE_REMOTE:
                for index, source in enumerate(files, 1):
                    dest = f"{RCLONE_REMOTE}/{source.stem}/{source.name}"
                    run_with_progress(["rclone", "copyto", str(source), dest,
                                       "--stats=5s", "--stats-one-line", "--stats-log-level", "NOTICE"],
                                      env, 0, "UPLOAD", state_path)
                    update_state(state_path, progress=int(index * 100 / len(files)))
            else:
                folder_id = env.get("DRIVE_FOLDER_ID")
                if not folder_id:
                    raise RuntimeError("DRIVE_FOLDER_ID is not configured")
                run_with_progress(
                    ["node", str(DRIVE_UPLOAD), folder_id, *[str(x) for x in files]],
                    env, 0, "UPLOAD", state_path,
                )
            update_state(state_path, phase="completed", status="completed", progress=100)
            print("==> all selected files uploaded", flush=True)
        except Exception as exc:
            update_state(state_path, phase="failed", status="failed", error=str(exc))
            raise
        return

    upload = bool(config.get("upload", True))
    keep_local = bool(config.get("keep_local", False))
    update_state(state_path, phase="validating", status="running", files=[str(x) for x in files])
    try:
        for index, source in enumerate(files, 1):
            total = validate(source)
            print(f"==> [{index}/{len(files)}] preparing HLS: {source.name}", flush=True)
            if upload and not keep_local:
                context = tempfile.TemporaryDirectory(prefix="bili-hls-")
                parent = Path(context.name)
            else:
                context = None
                HLS_ROOT.mkdir(parents=True, exist_ok=True)
                parent = HLS_ROOT
            output = parent / source.stem.rstrip(" .")
            update_state(state_path, phase="hls", current_file=str(source), progress=0)
            run_with_progress(["bash", str(PREP), str(source), str(output)], env, total, "HLS", state_path)
            if upload:
                update_state(state_path, phase="upload", progress=0)
                print(f"==> uploading: {source.stem}", flush=True)
                if RCLONE_REMOTE:
                    run_with_progress(["rclone", "copy", str(output), f"{RCLONE_REMOTE}/{output.name}",
                                       "--stats=5s", "--stats-one-line", "--stats-log-level", "NOTICE"],
                                      env, 0, "UPLOAD", state_path)
                else:
                    folder_id = env.get("DRIVE_FOLDER_ID")
                    if not folder_id:
                        raise RuntimeError("DRIVE_FOLDER_ID is not configured")
                    run_with_progress(["node", str(PUSH), str(output), folder_id], env, 0, "UPLOAD", state_path)
            if context:
                context.cleanup()
        update_state(state_path, phase="completed", status="completed", progress=100)
        print("==> all selected files completed", flush=True)
    except Exception as exc:
        update_state(state_path, phase="failed", status="failed", error=str(exc))
        raise


if __name__ == "__main__":
    main()
