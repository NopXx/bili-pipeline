# bili-pipeline

Self-hosted pipeline that downloads video, converts it to browser-playable HLS, and pushes the result to Google Drive for a private media library. Driven by a small web UI (Vue via CDN) behind a token-guarded Python control server.

## What it does

1. **Download** — Bilibili (via `bili_pull.py` / `pull.sh`) or **BitTorrent** (`torrent_download.py`, aria2: magnet or `.torrent`, resume/DHT/PEX).
2. **Convert to HLS** — `public/prep-hls.sh` (the [hls-prep](https://github.com/NopXx/hls-prep) engine, plus a `PREP_REQUIRE_NVENC` guard) builds an fMP4 HLS bundle: video copy or re-encode (incl. HDR tonemap / preserve / ladder), per-language audio renditions, WebVTT subtitles, poster, and a `schemaVersion 2` `*.info.json` manifest. `process_media.py` translates the web UI's modes into its knobs (a sized encode is a one-rung ladder; HDR Auto is `raw,1080,720`; Preserve HDR is an `hdr` rung).
3. **Upload to Drive** — each bundle (`drive_push.mjs`) or an original pre-HLS video (`drive_upload.mjs`, resumable) lands in its own Drive sub-folder, which the library treats as one series.
4. **Drive queue** — with `BILI_RCLONE_REMOTE` set, the Google Drive tab browses the remote and queues many videos at once. Each becomes its own chain: download → HLS (the profile chosen on that page) → upload beside the original → optionally delete the remote original, which happens only after the uploaded playlist is visible on the remote. A failed item never deletes anything.

Long-running work runs in independent queues: torrent inspection, download, HLS conversion, and Drive upload. Up to two downloads run concurrently by default; set `BILI_DOWNLOAD_CONCURRENCY` to an integer from 1 to 8 to change this. Inspection can read the next torrent's file list while downloads are active. A successful conversion enqueues a separate upload job, so upload can be paused while conversion continues. The UI shows the download, conversion, and upload lanes with progress and per-job controls.

On a host with two visible NVIDIA GPUs, two HLS conversions run at once, one per GPU. Each worker receives its own `CUDA_VISIBLE_DEVICES`; the queue shows the assigned GPU. With one or no detected GPU, conversion stays one-at-a-time. Set `BILI_CONVERT_GPUS=0,1` before starting the web server to choose the GPU IDs explicitly. Existing running jobs are not reassigned; restart the server only after they finish.

## Layout

- `scripts/bili_web.py` — HTTP control server (token-guarded, loopback; reach it over an SSH tunnel). Routes include queue status, pause/resume/cancel/retry, files, and `/api/log` (byte-range job log reads: tail, follow from an offset, page back with `before`, or `full` for download).
- `scripts/job_queue.py` — persistent per-lane scheduler; queued jobs survive a server restart.
- `scripts/process_media.py` — validates and converts to HLS; it does not upload inline.
- `scripts/upload_media.py` — independently uploads finished HLS bundles or original files.
- `scripts/{bili_pull.py,bili_login.py,pull.sh,torrent_download.py,drive_download.mjs}` — downloaders. Drive accepts a file link or ID, prefers the configured `BILI_RCLONE_REMOTE` (rclone `copyid`) and otherwise uses the existing OAuth connection. OAuth downloads resume a partial `.part` file on retry. Files land under `BILI_DOWNLOADS_DIR/drive/<file-id>/`.
- `scripts/{drive_push.mjs,drive_upload.mjs,drive_files.mjs}` — Google Drive I/O (googleapis).
- `public/prep-hls.sh` — the HLS builder (copied from hls-prep; update it from there).
- `scripts/remote_fetch.py` — Drive-queue downloader: rclone copy with an MKV header check before and ffprobe verification after.
- `lib/hls.js` — playlist/mime helpers shared by the uploaders.
- `frontend/` — the web UI (`index.html` + `assets/app.js`), served by `bili_web.py`.

## Setup

1. `npm install` (needs `googleapis`, `pg`).
2. `cp .env.example .env.local` and fill it in (Google OAuth, `DATABASE_URL`, `BILI_WEB_TOKEN`, …). **`.env.local` is git-ignored — never commit it.**
3. Install `ffmpeg`, `aria2`, and the Bilibili downloader dependencies.
4. Run the server: `BILI_WEB_TOKEN=… python3 scripts/bili_web.py` (binds `127.0.0.1:8787`).

## Notes

- The server binds loopback only; access it via an SSH tunnel.
- Bilibili login: the Bilibili download form shows the account status and a **QR-code login** (`scripts/bili_auth.py`, same web QR flow as `bili_login.py`). Scanning with the Bilibili app writes the session cookies into Bili23's `config.json`; cookies are never sent to the browser. Bili23 reads that file at start-up, so restart it after logging in — the UI offers a restart button, which runs `systemctl restart bili23.service` (override with `BILI23_RESTART_CMD`) and refuses while a Bilibili download is active.
- Set `BILI_TRANSFER_ONLY=1` to serve the download/upload-only web UI and reject HLS conversion requests. No GPU is needed. Upload is manual from the Files tab.
- Secrets live solely in `.env.local` and the Postgres `settings` table — nothing sensitive is committed.
- Pause/resume works for queued and running downloads/uploads. Running torrent and rclone workers are suspended as a process group; Bilibili jobs also ask Bili23's MCP server to pause/resume each task. Bilibili tasks cannot be paused before Bili23 has returned task IDs.
- A paused **running** job is marked interrupted if the web server restarts; retry it to resume a resumable torrent/rclone transfer. A paused **queued** job stays paused. Do not restart the server during active jobs when upgrading.
- Google Drive downloads support individual files, not folders. The OAuth path rejects Google Docs/Sheets/Slides; the configured `drive.file` grant only sees files this app created or that were explicitly opened/shared with this app. A share link alone may not grant API access. When `BILI_RCLONE_REMOTE` is configured, its Drive account and scope determine what can be downloaded; a URL `resourcekey` is passed through to rclone.
- HLS output stays under `BILI_HLS_DIR/<conversion-job-id>/` until all uploads succeed. Failed/cancelled uploads keep the files for retry. A successful upload removes them unless “เก็บ HLS” was selected.
- For a temporary Kaggle session, set `BILI_DOWNLOADS_DIR`, `BILI_HLS_DIR`, and optionally `BILI_JOBS_DIR` to writable directories. Set `BILI_RCLONE_REMOTE=metube:tube` to upload HLS bundles (and original files) through an existing private rclone config instead of the Node/OAuth uploader. This does not enable the Drive-list/delete API; that API still needs OAuth credentials. Bilibili downloads need a Bili23 MCP service: `scripts/bili23_headless.py install|start|restart|stop` runs Bili23 v2.20.0 headless (Qt offscreen, private venv under `BILI23_HOME`, downloads into `BILI_DOWNLOADS_DIR/bilibili`); point `BILI23_RESTART_CMD` at its `restart` so the web login's restart button works.
