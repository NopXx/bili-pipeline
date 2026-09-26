# bili-pipeline

Self-hosted pipeline that downloads video, converts it to browser-playable HLS, and pushes the result to Google Drive for a private media library. Driven by a small web UI (Vue via CDN) behind a token-guarded Python control server.

## What it does

1. **Download** — Bilibili (via `bili_pull.py` / `pull.sh`) or **BitTorrent** (`torrent_download.py`, aria2: magnet or `.torrent`, resume/DHT/PEX).
2. **Convert to HLS** — `public/prep-hls.sh` builds an fMP4 HLS bundle: video copy or re-encode (incl. HDR tonemap / preserve / ladder), per-language audio renditions, WebVTT subtitles, poster, and a `schemaVersion 2` `*.info.json` manifest (`public/write-info-json.py`).
3. **Upload to Drive** — each bundle (`drive_push.mjs`) or an original pre-HLS video (`drive_upload.mjs`, resumable) lands in its own Drive sub-folder, which the library treats as one series.

Long-running work runs as jobs with live progress (percent + upload speed), cancel, and retry.

## Layout

- `scripts/bili_web.py` — HTTP control server (token-guarded, loopback; reach it over an SSH tunnel). Routes: parse/pull/torrent/process/upload/status/cancel/jobs/files.
- `scripts/process_media.py` — orchestrates validate → HLS → upload, and the raw upload-only path.
- `scripts/{bili_pull.py,bili_login.py,pull.sh,torrent_download.py}` — downloaders.
- `scripts/{drive_push.mjs,drive_upload.mjs,drive_files.mjs}` — Google Drive I/O (googleapis).
- `public/{prep-hls.sh,write-info-json.py}` — the HLS builder and its manifest writer.
- `lib/hls.js` — playlist/mime helpers shared by the uploaders.
- `frontend/` — the web UI (`index.html` + `assets/app.js`), served by `bili_web.py`.

## Setup

1. `npm install` (needs `googleapis`, `pg`).
2. `cp .env.example .env.local` and fill it in (Google OAuth, `DATABASE_URL`, `BILI_WEB_TOKEN`, …). **`.env.local` is git-ignored — never commit it.**
3. Install `ffmpeg`, `aria2`, and the Bilibili downloader dependencies.
4. Run the server: `BILI_WEB_TOKEN=… python3 scripts/bili_web.py` (binds `127.0.0.1:8787`).

## Notes

- The server binds loopback only; access it via an SSH tunnel.
- Secrets live solely in `.env.local` and the Postgres `settings` table — nothing sensitive is committed.
- For a temporary Kaggle session, set `BILI_DOWNLOADS_DIR` and `BILI_HLS_DIR` to writable directories under `/kaggle/working`. Set `BILI_RCLONE_REMOTE=metube:tube` to upload HLS bundles (and original files) through an existing private rclone config instead of the Node/OAuth uploader. This does not enable the Drive-list/delete API; that API still needs OAuth credentials. Bilibili downloads still require a reachable Bili23 MCP service.
