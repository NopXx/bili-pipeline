#!/usr/bin/env bash
#
# One Bilibili link -> HLS bundles in Drive. Run on the VPS where Bili23 is
# running headless (see vps-setup.md).
#
#   scripts/pull.sh <bilibili-url-or-id>
#
# 1. bili_pull.py drives Bili23 over MCP: download every episode of the link.
# 2. prep-hls.sh turns each downloaded file into an HLS bundle.
# 3. drive_push.mjs uploads each bundle into DRIVE_FOLDER_ID.
# The next sync in the app lists them as drafts, ready to curate.
#
# prep-hls knobs are passed straight through the environment (PREP_LADDER, etc).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ $# -lt 1 ]; then
  echo "usage: $0 <bilibili-url-or-id>" >&2
  exit 1
fi

# DRIVE_FOLDER_ID etc. live in .env.local; drive_push.mjs reads it too, this is
# just so we can fail early if it is missing. Load it as a *fallback* — anything
# already in the environment (e.g. a per-job BILI_VIDEO_QUALITY from the picker
# UI) wins, so sourcing the file must not clobber it.
env_file="${BILI_ENV_FILE:-$here/.env.local}"
if [ -f "$env_file" ]; then
  while IFS='=' read -r k v; do
    case "$k" in ''|\#*) continue ;; esac
    [ -z "${!k+x}" ] && export "$k=$v"
  done < "$env_file"
fi
if [ "${BILI_DOWNLOAD_ONLY:-0}" != "1" ] && [ -z "${BILI_RCLONE_REMOTE:-}" ]; then
  : "${DRIVE_FOLDER_ID:?set DRIVE_FOLDER_ID in .env.local}"
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "==> downloading from Bilibili" >&2
mapfile -t files < <(python3 "$here/scripts/bili_pull.py" "$1")

if [ ${#files[@]} -eq 0 ]; then
  echo "nothing downloaded" >&2
  exit 1
fi

# The web workflow deliberately stops here: after the download finishes the
# user chooses an HLS profile, then starts conversion/upload as a separate job.
if [ "${BILI_DOWNLOAD_ONLY:-0}" = "1" ]; then
  if [ -n "${BILI_JOB_STATE:-}" ]; then
    python3 -c 'import json,os,sys; p=sys.argv[1]; d=json.load(open(p,encoding="utf-8")); d.update({"phase":"downloaded","status":"downloaded","files":sys.argv[2:]}); t=p+".tmp"; open(t,"w",encoding="utf-8").write(json.dumps(d,ensure_ascii=False,indent=2)+"\n"); os.replace(t,p)' "$BILI_JOB_STATE" "${files[@]}"
  fi
  echo "==> download complete. Choose an HLS profile in the web UI." >&2
  exit 0
fi

for src in "${files[@]}"; do
  [ -f "$src" ] || { echo "skip missing: $src" >&2; continue; }
  base="$(basename "${src%.*}")"

  # Guard the heavy path: prep-hls copies an H.264 stream but re-encodes anything
  # else (a 4K HEVC file pins both cores for ~an hour). Only proceed on H.264,
  # unless explicitly told to allow the re-encode.
  vcodec="$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 "$src" 2>/dev/null || true)"
  if [ "$vcodec" != "h264" ] && [ "${PREP_ALLOW_REENCODE:-0}" != "1" ]; then
    echo "skip: '$base' is $vcodec, not h264 — re-download with an H.264 quality, or set PREP_ALLOW_REENCODE=1 to force the re-encode." >&2
    continue
  fi

  bundle="$work/$base"
  mkdir -p "$bundle"
  echo "==> prep-hls ($vcodec): $src" >&2
  bash "$here/public/prep-hls.sh" "$src" "$bundle"
  echo "==> pushing to Drive: $base" >&2
  if [ -n "${BILI_RCLONE_REMOTE:-}" ]; then
    rclone copy "$bundle" "${BILI_RCLONE_REMOTE%/}/$base"
  else
    node "$here/scripts/drive_push.mjs" "$bundle" "$DRIVE_FOLDER_ID"
  fi
done

echo "==> all done. Open /admin and Sync to see them." >&2
