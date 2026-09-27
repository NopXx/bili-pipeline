#!/usr/bin/env python3
"""
Tiny episode-picker UI in front of pull.sh. Paste a Bilibili link, it parses the
episode list (via bili_pull.py --parse-only), you tick the ones you want, and it
runs the download -> HLS -> Drive pipeline for just those.

Runs on the Bili23 VPS. stdlib only. Gate it with a token:

    BILI_WEB_TOKEN=<secret> python3 scripts/bili_web.py        # binds 127.0.0.1:8787

Binds loopback by default — reach it over an SSH tunnel, so nothing is exposed
to the internet:  ssh -L 8787:localhost:8787 root@<vps>  then open localhost:8787.
The token is still required for every /api call (X-Token header). Set
BILI_WEB_HOST=0.0.0.0 only if you deliberately want it network-reachable.
"""
import base64
import binascii
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from job_queue import JobQueue
import bili_auth

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PULL = os.path.join(HERE, "scripts", "pull.sh")
BILI_PULL = os.path.join(HERE, "scripts", "bili_pull.py")
PROCESS_MEDIA = os.path.join(HERE, "scripts", "process_media.py")
UPLOAD_MEDIA = os.path.join(HERE, "scripts", "upload_media.py")
TORRENT_DOWNLOAD = os.path.join(HERE, "scripts", "torrent_download.py")
REMOTE_FETCH = os.path.join(HERE, "scripts", "remote_fetch.py")
DRIVE_FILES = os.path.join(HERE, "scripts", "drive_files.mjs")
DRIVE_DOWNLOAD = os.path.join(HERE, "scripts", "drive_download.mjs")
JOBS_DIR = os.path.realpath(os.environ.get("BILI_JOBS_DIR", os.path.join(HERE, "jobs")))
DOWNLOADS_DIR = os.path.realpath(os.environ.get("BILI_DOWNLOADS_DIR", "/opt/bili-downloads"))
TORRENT_DOWNLOADS_DIR = os.path.realpath(os.path.join(DOWNLOADS_DIR, "torrents"))
FRONTEND_DIR = os.path.realpath(os.path.join(HERE, "frontend"))
TOKEN = os.environ.get("BILI_WEB_TOKEN") or ""
HOST = os.environ.get("BILI_WEB_HOST", "127.0.0.1")  # loopback; tunnel in over SSH
PORT = int(os.environ.get("BILI_WEB_PORT", "8787"))
TRANSFER_ONLY = os.environ.get("BILI_TRANSFER_ONLY") == "1"
RCLONE_REMOTE = os.environ.get("BILI_RCLONE_REMOTE", "").rstrip("/")
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".mov", ".webm", ".m4v", ".avi", ".ts")
LOG_TAIL = 128 * 1024  # first view of a log
LOG_CHUNK = 512 * 1024  # one incremental or "older lines" read
LOG_FULL_LIMIT = 32 * 1024 * 1024  # log download
# Live transfer details a worker mirrors into its state file, passed to the UI.
# Values Bili23's create_download accepts (v2.20.0 media_info maps).
BILI_VIDEO_QUALITIES = ("auto", "8K", "DOLBY_VISION", "HDR", "4K_SDR", "4K", "1080P60", "1080P+", "AI", "1080P", "720P", "480P", "360P")
BILI_VIDEO_CODECS = ("auto", "AVC/H.264", "HEVC/H.265", "AV1")
BILI_AUDIO_QUALITIES = ("auto", "HI_RES", "DOLBY_ATMOS", "192K", "132K", "64K")
BILI_CONTAINERS = ("mp4", "mkv")
JOB_DETAIL_KEYS = ("phase", "error", "exit_code", "speed", "eta", "done", "total", "peers",
                   "downloaded_bytes", "total_bytes", "speed_bytes", "current_file", "file_index", "file_count",
                   "destination", "title")

os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(TORRENT_DOWNLOADS_DIR, exist_ok=True)
def enqueue_upload(paths, kind, keep_local=False, parent_job=None):
    job = secrets.token_hex(8)
    config = {"kind": kind, "paths": paths, "keep_local": bool(keep_local)}
    config_path = os.path.join(JOBS_DIR, job + ".config.json")
    with open(config_path, "w", encoding="utf-8") as out:
        json.dump(config, out, ensure_ascii=False, indent=2)
    with open(os.path.join(JOBS_DIR, job + ".meta.json"), "w", encoding="utf-8") as out:
        json.dump({"job": job, "kind": "upload", "upload_kind": kind, "parent_job": parent_job,
                   "name": os.path.basename(paths[0]) if paths else "upload",
                   "created": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), **config},
                  out, ensure_ascii=False, indent=2)
    queue.submit(job, "upload", [sys.executable, UPLOAD_MEDIA, config_path, os.path.join(JOBS_DIR, job + ".json")])
    return job


HLS_OPTION_KEYS = {
    "copy_video", "reencode", "ladder", "auto_hdr", "preserve_hdr",
    "ladder_heights", "ladder_bitrates", "height", "video_bitrate",
    "audio_channels", "audio_bitrate", "segment_seconds", "poster_seconds",
    "gpu_tonemap", "copy_audio", "upload", "keep_local",
}


def enqueue_process(body, pipeline=None):
    """Queue an HLS conversion of downloaded files; `pipeline` rides along in meta."""
    files = [str(item) for item in (body.get("files") or []) if item]
    if not files:
        raise ValueError("no files selected")
    for path in files:
        resolved = os.path.realpath(path)
        if not resolved.startswith(DOWNLOADS_DIR + os.sep) or not os.path.isfile(resolved):
            raise ValueError(f"invalid downloaded file: {path}")
    config = {key: body[key] for key in HLS_OPTION_KEYS if key in body}
    config["files"] = [os.path.realpath(path) for path in files]
    job = secrets.token_hex(8)
    state_path = os.path.join(JOBS_DIR, job + ".json")
    config_path = os.path.join(JOBS_DIR, job + ".config.json")
    created = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({"phase": "queued", "status": "queued", "files": config["files"]}, f, ensure_ascii=False, indent=2)
    meta = {"job": job, "kind": "hls", "created": created, **config}
    if pipeline:
        meta["pipeline"] = pipeline
    with open(os.path.join(JOBS_DIR, job + ".meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    queue.submit(job, "convert", [sys.executable, PROCESS_MEDIA, config_path, state_path])
    return job


def remote_relative(value):
    """Normalise a path under BILI_RCLONE_REMOTE; refuse anything that climbs out."""
    parts = [part for part in str(value or "").replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("invalid remote path")
    return "/".join(parts)


def remote_spec(relative):
    return f"{RCLONE_REMOTE}/{relative}" if relative else RCLONE_REMOTE


def enqueue_remote_download(relative, pipeline):
    """One Drive-queue item: download (remote_fetch.py), then per `pipeline`
    convert, upload and optionally delete the remote original."""
    job = secrets.token_hex(8)
    config_path = os.path.join(JOBS_DIR, job + ".config.json")
    state_path = os.path.join(JOBS_DIR, job + ".json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({"source": remote_spec(relative), "destination": os.path.join(DOWNLOADS_DIR, "remote", job)},
                  f, ensure_ascii=False, indent=2)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({"kind": "remote_download", "phase": "queued", "status": "queued", "progress": 0}, f)
    with open(os.path.join(JOBS_DIR, job + ".meta.json"), "w", encoding="utf-8") as f:
        json.dump({"job": job, "kind": "remote_download", "source": relative, "url": relative,
                   "name": os.path.basename(relative), "pipeline": pipeline,
                   "created": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}, f, ensure_ascii=False, indent=2)
    queue.submit(job, "download", [sys.executable, REMOTE_FETCH, config_path, state_path])
    return job


def read_json(path):
    try:
        with open(path, encoding="utf-8") as source:
            return json.load(source)
    except (OSError, ValueError):
        return {}


def append_log(item, message):
    with open(item["log"], "a", encoding="utf-8") as out:
        out.write(f"==> {message}\n")


def finish_remote_pipeline(upload_item, pipeline, bundles):
    """After a Drive-queue item's HLS upload: remove the local original and,
    when asked, delete the remote original — but only once each uploaded
    bundle's playlist is visible on the remote."""
    remote_root = os.path.join(DOWNLOADS_DIR, "remote") + os.sep
    for path in pipeline.get("local_files") or []:
        resolved = os.path.realpath(path)
        if resolved.startswith(remote_root) and os.path.isfile(resolved):
            os.remove(resolved)
            append_log(upload_item, f"removed local original {resolved}")
            try:
                os.rmdir(os.path.dirname(resolved))
            except OSError:
                pass
    source = pipeline.get("remote_source")
    if not (pipeline.get("delete_remote_source") and source and RCLONE_REMOTE):
        return
    for bundle in bundles:
        name = os.path.basename(str(bundle).rstrip("/"))
        listing = subprocess.run(["rclone", "lsf", remote_spec(name), "--include", "*.m3u8"],
                                 capture_output=True, text=True, timeout=120)
        if listing.returncode or not listing.stdout.strip():
            append_log(upload_item, f"kept remote original {source}: no uploaded playlist found in {name}")
            return
    result = subprocess.run(["rclone", "deletefile", remote_spec(source)], capture_output=True, text=True, timeout=120)
    if result.returncode:
        append_log(upload_item, f"could not delete remote original {source}: {result.stderr.strip()[-300:]}")
    else:
        append_log(upload_item, f"deleted remote original {source}")


def on_job_complete(item, code):
    if code:
        return
    meta = read_json(item["meta"])
    pipeline = meta.get("pipeline") or {}
    if item["lane"] == "download" and pipeline.get("hls") is not None:
        # A Drive-queue download: convert it with the chosen profile; the
        # convert branch below then queues its upload.
        files = read_json(item["state"]).get("video_files") or []
        try:
            child = enqueue_process({**pipeline["hls"], "files": files, "upload": True},
                                    pipeline={**pipeline, "remote_source": meta.get("source"), "local_files": files})
            queue._state(item, convert_job=child)
            append_log(item, f"queued HLS conversion job {child}")
        except Exception as exc:
            append_log(item, f"could not queue HLS conversion: {exc}")
        return
    if item["lane"] == "upload" and meta.get("upload_kind") == "hls" and meta.get("parent_job"):
        parent = read_json(os.path.join(JOBS_DIR, meta["parent_job"] + ".meta.json")).get("pipeline")
        if parent:
            # rclone round trips; keep them off the scheduler thread.
            threading.Thread(target=finish_remote_pipeline, args=(item, parent, meta.get("paths") or []),
                             name="bili-remote-finish", daemon=True).start()
        return
    on_convert_complete(item, code)


def on_convert_complete(item, code):
    if code or item["lane"] != "convert":
        return
    try:
        with open(item["meta"], encoding="utf-8") as source:
            meta = json.load(source)
        if not meta.get("upload"):
            return
        with open(item["state"], encoding="utf-8") as source:
            state = json.load(source)
        outputs = state.get("outputs") or []
        if not outputs:
            return
        child = enqueue_upload(outputs, "hls", meta.get("keep_local", False), item["job"])
        queue._state(item, upload_job=child)
        with open(item["log"], "a", encoding="utf-8") as out:
            out.write(f"==> queued separate Drive upload job {child}\n")
    except Exception as exc:
        queue._state(item, upload_queue_error=str(exc))
        with open(item["log"], "a", encoding="utf-8") as out:
            out.write(f"==> could not queue upload: {exc}\n")


queue = JobQueue(JOBS_DIR, on_job_complete)
jobs = queue.jobs

PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Bili -> Drive</title><style>
:root{color-scheme:dark}
body{background:#0d0f13;color:#e6e8eb;font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px;max-width:900px;margin:0 auto}
h1{font-size:18px;margin:0 0 16px}
input,button{font:inherit}
input[type=text]{width:100%;box-sizing:border-box;padding:10px;background:#171a1f;border:1px solid #2a2f37;border-radius:8px;color:inherit}
button{padding:9px 16px;background:#2563eb;border:0;border-radius:8px;color:#fff;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
button.sec{background:#2a2f37}
.row{display:flex;gap:8px;margin:12px 0}
.ep{display:flex;gap:10px;align-items:center;padding:7px 10px;border-bottom:1px solid #1c2027}
.ep:hover{background:#141821}
.ep .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ep .d{color:#8b93a1;font-size:13px}
.bar{display:flex;gap:8px;align-items:center;margin:12px 0}
#list{border:1px solid #2a2f37;border-radius:8px;max-height:52vh;overflow:auto;margin:8px 0}
label.sa{color:#8b93a1;font-size:14px;display:flex;gap:6px;align-items:center}
pre{background:#0a0c10;border:1px solid #1c2027;border-radius:8px;padding:12px;overflow:auto;max-height:40vh;white-space:pre-wrap;font-size:13px}
.msg{color:#f59e0b;min-height:20px}
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tab{background:#171a1f}
.tab.active{background:#2563eb}
button.danger{background:#b91c1c}
.flist{border:1px solid #2a2f37;border-radius:8px;max-height:44vh;overflow:auto;margin:8px 0}
.fhead{display:flex;gap:8px;align-items:center;margin-top:8px}
.fsec{margin-bottom:22px}
.status{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:12px 0}
.card{background:#141821;border:1px solid #2a2f37;border-radius:8px;padding:10px}
.card small{display:block;color:#8b93a1}.ok{color:#34d399}.bad{color:#f87171}
.progress{height:10px;background:#20252d;border-radius:99px;overflow:hidden;margin:8px 0;display:none}
.progress>div{height:100%;width:0;background:#2563eb;transition:width .3s}
.job-actions{display:flex;gap:6px}.job-actions button{padding:5px 9px}
.config{background:#141821;border:1px solid #2a2f37;border-radius:10px;padding:14px;margin:12px 0}
.config-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.config label{display:flex;flex-direction:column;gap:4px;color:#aeb5c0}
.config select,.config input[type=text],.config input[type=number]{background:#171a1f;color:inherit;border:1px solid #2a2f37;border-radius:6px;padding:7px}
.details{margin:10px 0;display:grid;gap:10px}.media-card{background:#11151b;border:1px solid #2a2f37;border-radius:8px;padding:12px}
.media-card h3{font-size:14px;margin:0 0 8px;overflow-wrap:anywhere}.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:6px}
.fact{background:#171a1f;border-radius:6px;padding:7px}.fact small{display:block;color:#8b93a1}
.tracks{width:100%;border-collapse:collapse;margin-top:10px;font-size:12px}.tracks th,.tracks td{text-align:left;padding:6px;border-bottom:1px solid #2a2f37}.tracks th{color:#8b93a1}
</style></head><body>
<h1>Bilibili &rarr; HLS &rarr; Drive</h1>
<div class=row>
  <input id=token type=password placeholder="Access token">
  <button class=sec id=save-token>Save token</button>
</div>
<div class=status id=system-status></div>
<div class=tabs>
  <button class="tab active" data-tab=download>Download</button>
  <button class=tab data-tab=files>Files</button>
  <button class=tab data-tab=jobs>Jobs</button>
</div>
<div id=tab-download>
<div class=row>
  <input id=url type=text placeholder="https://www.bilibili.com/video/BV... or a media id">
  <button id=parse>Parse</button>
</div>
<div class=msg id=msg></div>
<div class=bar id=bar style=display:none>
  <label class=sa><input type=checkbox id=all> Select all</label>
  <span id=count style=color:#8b93a1></span>
  <span style=flex:1></span>
  <label class=sa>Quality
    <select id=quality style="background:#171a1f;color:inherit;border:1px solid #2a2f37;border-radius:6px;padding:5px">
      <option value=4K selected>4K (H.264 SDR, no re-encode)</option>
      <option value=1080P>1080p</option>
      <option value=720P>720p</option>
      <option value=480P>480p</option>
      <option value=auto>Best available</option>
    </select></label>
  <label class=sa><input type=checkbox id=redl> Force redownload</label>
  <button id=go>Download selected</button>
  <button class=danger id=cancel style=display:none>Cancel</button>
</div>
<div id=list></div>
<div class=progress id=progress><div></div></div>
<div id=progress-label class=msg></div>
<pre id=log style=display:none></pre>
<div class=config id=hls-config hidden>
  <b>2. HLS configuration</b>
  <div id=ready-files class=msg></div>
  <div id=convert-details class=details></div>
  <div id=size-estimate class=msg></div>
  <div class=config-grid>
    <label>Video mode<select id=hls-mode><option value=copy>Copy H.264 (fastest)</option><option value=encode>Re-encode H.264</option><option value=ladder>Adaptive ladder</option></select></label>
    <label>Ladder heights<input id=hls-heights type=text value="2160,1440,1080"></label>
    <label>Video bitrate<input id=hls-vb type=text value="8M"></label>
    <label>Audio outputs<select id=hls-audio><option value=2>Stereo AAC</option><option value=2,6>Stereo + 5.1 AAC</option><option value=2,6,raw>Stereo + 5.1 + original</option><option value=raw>Original audio</option></select></label>
    <label>Audio bitrate<input id=hls-ab type=text value="192k"></label>
    <label>Segment seconds<input id=hls-seg type=number min=2 max=30 value=6></label>
    <label>Poster at second<input id=hls-poster type=number min=0 value=5></label>
  </div>
  <div class=row><label class=sa><input id=hls-gpu type=checkbox> GPU tonemap HDR</label><label class=sa><input id=hls-upload type=checkbox checked> Upload to Drive</label><label class=sa><input id=hls-keep type=checkbox> Keep HLS on VPS</label><span style=flex:1></span><button id=hls-start>Convert HLS</button></div>
</div>
</div>
<div id=tab-files hidden>
  <div class=fsec>
    <div class=fhead><b>Downloads on VPS</b> <span id=dlsize style=color:#8b93a1></span>
      <span style=flex:1></span>
      <button class=sec id=dlrefresh>Refresh</button>
      <button class=sec id=dlinspect>Inspect selected</button>
      <button id=dlconvert>Convert selected to HLS</button>
      <button class=danger id=dldel>Delete selected</button></div>
    <div class=msg id=dlmsg></div>
    <div id=file-details class=details></div>
    <div id=dllist class=flist></div>
  </div>
  <div class=fsec>
    <div class=fhead><b>Drive library folder</b> <span id=drsize style=color:#8b93a1></span>
      <span style=flex:1></span>
      <button class=sec id=drrefresh>Refresh</button>
      <button class=danger id=drdel>Delete selected</button></div>
    <div class=msg id=drmsg></div>
    <div id=drlist class=flist></div>
</div>
</div>
<div id=tab-jobs hidden>
  <div class=fhead><b>Recent jobs</b><span style=flex:1></span><button class=sec id=jobrefresh>Refresh</button></div>
  <div class=msg id=jobmsg></div><div id=joblist class=flist></div>
</div>
<script>
const $=s=>document.querySelector(s)
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))
let TOKEN=localStorage.getItem('bwt')||''
let ACTIVE_JOB=localStorage.getItem('bwj')||''
let READY_FILES=[]
let PROBE_DATA=[]
$('#token').value=TOKEN
$('#save-token').onclick=()=>{
  TOKEN=$('#token').value.trim()
  if(TOKEN) localStorage.setItem('bwt',TOKEN)
  else localStorage.removeItem('bwt')
  health()
}
function ensureToken(){
  if(!TOKEN) throw new Error('Enter the access token above, then press Save token')
  return TOKEN
}
async function api(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Token':ensureToken()},body:JSON.stringify(body||{})})
  if(r.status===401){ localStorage.removeItem('bwt'); TOKEN=''; throw new Error('bad token — reload and re-enter') }
  const j=await r.json(); if(!r.ok||j.error) throw new Error(j.error||('HTTP '+r.status)); return j
}
let eps=[]
$('#parse').onclick=async()=>{
  const url=$('#url').value.trim(); if(!url)return
  $('#msg').textContent='parsing…'; $('#bar').style.display='none'; $('#list').innerHTML=''
  try{ eps=await api('/api/parse',{url}); render() }catch(e){ $('#msg').textContent=e.message }
}
function render(){
  $('#msg').textContent=''
  if(!eps.length){ $('#msg').textContent='no episodes'; return }
  $('#list').innerHTML=eps.map((e,i)=>`<div class=ep><input type=checkbox class=cb data-i=${i} ${e.needs_reparse?'disabled':''}>
    <span class=t>${esc(e.title||('#'+(i+1)))}</span><span class=d>${esc(e.duration||'')}</span></div>`).join('')
  // default to the whole collection selected — deselect to trim
  document.querySelectorAll('.cb:not(:disabled)').forEach(c=>c.checked=true)
  $('#all').checked=true
  $('#bar').style.display='flex'; updateCount()
  document.querySelectorAll('.cb').forEach(c=>c.onchange=updateCount)
}
function selected(){ return [...document.querySelectorAll('.cb:checked')].map(c=>eps[+c.dataset.i].episode_id) }
function updateCount(){ $('#count').textContent=selected().length+' / '+eps.length }
$('#all').onchange=e=>{ document.querySelectorAll('.cb:not(:disabled)').forEach(c=>c.checked=e.target.checked); updateCount() }
$('#go').onclick=async()=>{
  const ids=selected(); if(!ids.length){ $('#msg').textContent='select at least one'; return }
  $('#go').disabled=true; $('#log').style.display='block'; $('#log').textContent='starting…'
  try{
    const {job}=await api('/api/pull',{url:$('#url').value.trim(),episode_ids:ids,quality:$('#quality').value,redownload:$('#redl').checked})
    ACTIVE_JOB=job; localStorage.setItem('bwj',job)
    $('#cancel').style.display='inline-block'; $('#cancel').disabled=false; $('#cancel').dataset.job=job
    poll(job)
  }catch(e){ $('#log').textContent=e.message; $('#go').disabled=false }
}
$('#cancel').onclick=async()=>{
  const job=$('#cancel').dataset.job
  if(!job||!confirm('Cancel this download and delete its partial files?'))return
  $('#cancel').disabled=true
  try{
    const s=await api('/api/cancel',{job})
    $('#log').textContent=s.log||'Cancelled'; $('#go').disabled=false; $('#cancel').style.display='none'
  }catch(e){ $('#log').textContent+='\\nCancel failed: '+e.message; $('#cancel').disabled=false }
}
async function poll(job){
  try{
    const s=await api('/api/status',{job})
    $('#log').textContent=s.log||''; $('#log').scrollTop=$('#log').scrollHeight
    updateProgress(s.log||'',s.running,s.exit_code,s.cancelled)
    if(!s.running&&s.state&&s.state.phase==='downloaded'&&s.state.files?.length){ showHlsConfig(s.state.files) }
    if(s.running){ setTimeout(()=>poll(job),1500) } else { $('#go').disabled=false; $('#cancel').style.display='none'; ACTIVE_JOB=''; localStorage.removeItem('bwj'); health() }
  }catch(e){ $('#log').textContent+='\\n'+e.message; $('#go').disabled=false; $('#cancel').style.display='none' }
}
async function showHlsConfig(files){
  READY_FILES=files; $('#hls-config').hidden=false
  $('#ready-files').textContent=files.length+' file(s) downloaded — choose settings before conversion'
  $('#convert-details').innerHTML='<div class=msg>Reading media information…</div>'
  try{ PROBE_DATA=await api('/api/probe',{paths:files}); $('#convert-details').innerHTML=renderMedia(PROBE_DATA); updateEstimate() }
  catch(e){ $('#convert-details').innerHTML='<div class=msg>'+esc(e.message)+'</div>' }
  $('#hls-config').scrollIntoView({behavior:'smooth',block:'start'})
}
$('#hls-mode').onchange=()=>{ $('#hls-heights').disabled=$('#hls-mode').value!=='ladder'; updateEstimate() }
$('#hls-mode').onchange()
$('#hls-start').onclick=async()=>{
  if(!READY_FILES.length)return
  $('#hls-start').disabled=true
  const mode=$('#hls-mode').value
  const config={files:READY_FILES,copy_video:mode==='copy',reencode:mode==='encode',ladder:mode==='ladder',ladder_heights:$('#hls-heights').value.trim(),video_bitrate:$('#hls-vb').value.trim(),audio_channels:$('#hls-audio').value,audio_bitrate:$('#hls-ab').value.trim(),segment_seconds:+$('#hls-seg').value,poster_seconds:+$('#hls-poster').value,gpu_tonemap:$('#hls-gpu').checked,copy_audio:$('#hls-audio').value.includes('raw'),upload:$('#hls-upload').checked,keep_local:$('#hls-keep').checked}
  try{ const r=await api('/api/process',config); $('#hls-config').hidden=true; ACTIVE_JOB=r.job; localStorage.setItem('bwj',r.job); $('#cancel').style.display='inline-block'; $('#cancel').dataset.job=r.job; poll(r.job) }
  catch(e){ $('#progress-label').textContent=e.message; $('#hls-start').disabled=false }
}
function updateProgress(log,running,exitCode,cancelled){
  const matches=[...log.matchAll(/\\|\\s*(\\d{1,3})%\\s*\\|/g)], pct=matches.length?Math.min(100,+matches.at(-1)[1]):0
  $('#progress').style.display=running||pct?'block':'none'; $('#progress>div').style.width=pct+'%'
  let stage=''; if(/uploading:|pushing to Drive/i.test(log))stage='Uploading to Drive'; else if(/preparing HLS|HLS \\|/i.test(log))stage='Preparing HLS'; else if(/merg|ffmpeg_queued/i.test(log))stage='Merging'; else if(running)stage='Downloading'
  if(!running)stage=cancelled?'Cancelled':(exitCode===0?'Completed':'Failed')
  $('#progress-label').textContent=(stage||'')+(pct&&stage==='Downloading'?' · '+pct+'%':'')
}

// ---- tabs ----
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===b))
  $('#tab-download').hidden = b.dataset.tab!=='download'
  $('#tab-files').hidden = b.dataset.tab!=='files'
  $('#tab-jobs').hidden = b.dataset.tab!=='jobs'
  if(b.dataset.tab==='files'){ loadDownloads(); loadDrive() }
  if(b.dataset.tab==='jobs')loadJobs()
})
function fmt(n){ n=+n||0; const u=['B','KB','MB','GB']; let i=0; while(n>=1024&&i<3){n/=1024;i++} return n.toFixed(i?1:0)+u[i] }
function fmtRate(n){ n=+n||0; return n?(n/1000000).toFixed(2)+' Mbps':'unknown' }
function fmtDur(n){ n=Math.round(+n||0); return [Math.floor(n/3600),Math.floor(n%3600/60),n%60].map(x=>String(x).padStart(2,'0')).join(':') }
function fmtFps(v){ const p=String(v||'').split('/').map(Number); return p.length===2&&p[1]?(p[0]/p[1]).toFixed(3).replace(/0+$/,'').replace(/\\.$/,''):String(v||'?') }
function renderMedia(rows){ return rows.map(m=>`<div class=media-card><h3>${esc(m.name)}</h3><div class=facts>
  <div class=fact><small>Size / duration</small>${fmt(m.size)} · ${fmtDur(m.duration)}</div>
  <div class=fact><small>Video</small>${esc(m.video.codec||'none')} ${esc(m.video.profile||'')} · ${m.video.width||'?'}×${m.video.height||'?'}</div>
  <div class=fact><small>Video bitrate</small>${fmtRate(m.video.bit_rate)}</div>
  <div class=fact><small>Color</small>${esc(m.video.pix_fmt||'?')} · ${m.video.hdr?'HDR':'SDR'}${m.video.dolby_vision?' · Dolby Vision':''}</div>
  <div class=fact><small>Frame rate</small>${fmtFps(m.video.fps)} fps</div>
  <div class=fact><small>Subtitles</small>${m.subtitles.length?m.subtitles.map(s=>esc(s.language||s.codec)).join(', '):'none'}</div></div>
  ${m.audio.length?`<table class=tracks><thead><tr><th>#</th><th>Language / title</th><th>Codec</th><th>Channels</th><th>Sample</th><th>Bitrate</th><th>First packet</th></tr></thead><tbody>${m.audio.map(a=>`<tr><td>${a.index}</td><td>${esc(a.language||'und')} · ${esc(a.title||'')}</td><td>${esc(a.codec)} ${esc(a.profile||'')}</td><td>${a.channels||'?'} · ${esc(a.layout||'')}</td><td>${a.sample_rate?Math.round(a.sample_rate/1000)+' kHz':'?'}</td><td>${fmtRate(a.bit_rate)}</td><td>${(+a.first_packet||0).toFixed(3)}s</td></tr>`).join('')}</tbody></table>`:'<div class=msg>No audio streams</div>'}</div>`).join('') }
function parseRate(s){ const m=String(s).trim().match(/^(\\d+(?:\\.\\d+)?)([mk])?$/i); if(!m)return 0; return +m[1]*(m[2]?.toLowerCase()==='m'?1e6:m[2]?1e3:1) }
function updateEstimate(){
  if(!PROBE_DATA.length)return
  const mode=$('#hls-mode').value, vb=parseRate($('#hls-vb').value), ab=parseRate($('#hls-ab').value)||192000
  let bytes=0
  for(const m of PROBE_DATA){ let video=mode==='copy'?(+m.video.bit_rate||(+m.bit_rate||0)):vb
    if(mode==='ladder'){ const rates={2160:16000000,1440:10000000,1080:8000000,720:4000000,480:2000000}; video=$('#hls-heights').value.split(',').reduce((n,h)=>n+(rates[+h]||0),0) }
    const specs=$('#hls-audio').value.split(','), audio=specs.reduce((n,x)=>n+(x==='raw'?(+m.audio[0]?.bit_rate||0):ab),0)
    bytes+=(video+audio)*(+m.duration||0)/8
  }
  $('#size-estimate').textContent='Estimated HLS size: '+fmt(bytes)+' (approximate)'
}
function fileRow(val,label,size){ return `<div class=ep><input type=checkbox class=fcb value="${encodeURIComponent(val)}">
  <span class=t>${esc(label)}</span><span class=d>${fmt(size)}</span></div>` }
function picked(container){ return [...document.querySelectorAll(container+' .fcb:checked')].map(c=>decodeURIComponent(c.value)) }

// ---- downloads (VPS) ----
async function loadDownloads(){
  $('#dlmsg').textContent='loading…'; $('#dllist').innerHTML=''
  try{
    const files=await api('/api/files/downloads')
    $('#dlsize').textContent=files.length+' files · '+fmt(files.reduce((a,f)=>a+ (+f.size||0),0))
    $('#dllist').innerHTML=files.map(f=>fileRow(f.path,f.path.replace(/^.*\\/bili-downloads\\//,''),f.size)).join('')||'<div class=ep>empty</div>'
    $('#dlmsg').textContent=''
  }catch(e){ $('#dlmsg').textContent=e.message }
}
$('#dlrefresh').onclick=loadDownloads
$('#dlinspect').onclick=async()=>{
  const paths=picked('#dllist'); if(!paths.length){ $('#dlmsg').textContent='Select at least one file'; return }
  $('#file-details').innerHTML='<div class=msg>Reading media information…</div>'
  try{ const rows=await api('/api/probe',{paths}); $('#file-details').innerHTML=renderMedia(rows); $('#dlmsg').textContent='' }
  catch(e){ $('#file-details').innerHTML=''; $('#dlmsg').textContent=e.message }
}
$('#dlconvert').onclick=()=>{
  const paths=picked('#dllist')
  if(!paths.length){ $('#dlmsg').textContent='Select at least one video file'; return }
  const videoExt=/\\.(mp4|mkv|mov|webm|m4v)$/i
  const unsupported=paths.filter(path=>!videoExt.test(path))
  if(unsupported.length){ $('#dlmsg').textContent='Select finished video files only (.mp4, .mkv, .mov, .webm, .m4v)'; return }
  document.querySelector('[data-tab=download]').click()
  showHlsConfig(paths)
}
$('#dldel').onclick=async()=>{
  const paths=picked('#dllist'); if(!paths.length)return
  if(!confirm('Delete '+paths.length+' file(s) from the VPS? This cannot be undone.'))return
  try{ const r=await api('/api/delete/downloads',{paths}); $('#dlmsg').textContent='deleted '+r.deleted+(r.failed?(' · failed '+r.failed):''); loadDownloads() }
  catch(e){ $('#dlmsg').textContent=e.message }
}

// ---- drive ----
async function loadDrive(){
  $('#drmsg').textContent='loading…'; $('#drlist').innerHTML=''
  try{
    const files=await api('/api/files/drive')
    // group flat bundle files by their shared base name (dot-free by design)
    const g={}
    for(const f of files){ const base=f.name.split('.')[0]; (g[base]=g[base]||{ids:[],size:0,count:0}); g[base].ids.push(f.id); g[base].size+=+f.size||0; g[base].count++ }
    const groups=Object.entries(g)
    $('#drsize').textContent=files.length+' files · '+groups.length+' bundles · '+fmt(files.reduce((a,f)=>a+(+f.size||0),0))
    $('#drlist').innerHTML=groups.map(([base,x])=>fileRow(x.ids.join(','),base+'  ('+x.count+' files)',x.size)).join('')||'<div class=ep>empty</div>'
    $('#drmsg').textContent=''
  }catch(e){ $('#drmsg').textContent=e.message }
}
$('#drrefresh').onclick=loadDrive
$('#drdel').onclick=async()=>{
  const groups=picked('#drlist'); if(!groups.length)return
  const ids=groups.join(',').split(',').filter(Boolean)
  if(!confirm('Delete '+groups.length+' bundle(s) ('+ids.length+' files) from Drive? This cannot be undone.'))return
  try{ const r=await api('/api/delete/drive',{ids}); $('#drmsg').textContent='deleted '+r.deleted+(r.failed?(' · failed '+r.failed):''); loadDrive() }
  catch(e){ $('#drmsg').textContent=e.message }
}
async function health(){
  if(!TOKEN){ $('#system-status').innerHTML='<div class=card><small>System</small>Enter access token</div>'; return }
  try{ const h=await api('/api/health'); $('#system-status').innerHTML=`
    <div class=card><small>Bili23</small><span class=${h.bili23==='active'?'ok':'bad'}>${esc(h.bili23)}</span></div>
    <div class=card><small>Disk free</small>${fmt(h.disk_free)} / ${fmt(h.disk_total)}</div>
    <div class=card><small>Active jobs</small>${h.active_jobs}</div>`
  }catch(e){ $('#system-status').innerHTML='<div class=card><small>System</small><span class=bad>'+esc(e.message)+'</span></div>' }
}
async function loadJobs(){
  $('#jobmsg').textContent='loading…'
  try{ const rows=await api('/api/jobs'); $('#joblist').innerHTML=rows.map(j=>`<div class=ep>
    <span class=t><b>${esc(j.status)}</b> · ${esc(j.url||j.job)}<br><span class=d>${esc(j.created||'')} · ${fmt(j.log_size)}</span></span>
    <span class=job-actions><button class=sec data-open=${j.job}>Log</button>${j.url?`<button data-retry=${j.job}>Run again</button>`:''}</span></div>`).join('')||'<div class=ep>No jobs yet</div>'
    document.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>openJob(b.dataset.open))
    document.querySelectorAll('[data-retry]').forEach(b=>b.onclick=()=>retryJob(b.dataset.retry))
    $('#jobmsg').textContent=''
  }catch(e){ $('#jobmsg').textContent=e.message }
}
async function openJob(job){
  const s=await api('/api/status',{job}); document.querySelector('[data-tab=download]').click()
  $('#log').style.display='block'; $('#log').textContent=s.log||''; updateProgress(s.log||'',s.running,s.exit_code,s.cancelled)
  if(s.running){ ACTIVE_JOB=job; localStorage.setItem('bwj',job); $('#go').disabled=true; $('#cancel').style.display='inline-block'; $('#cancel').dataset.job=job; poll(job) }
}
async function retryJob(job){
  if(!confirm('Run this job again?'))return
  const r=await api('/api/retry',{job}); ACTIVE_JOB=r.job; localStorage.setItem('bwj',r.job); document.querySelector('[data-tab=download]').click()
  $('#go').disabled=true; $('#log').style.display='block'; $('#cancel').style.display='inline-block'; $('#cancel').dataset.job=r.job; poll(r.job)
}
$('#jobrefresh').onclick=loadJobs
for(const id of ['hls-vb','hls-ab','hls-audio','hls-heights'])$('#'+id).addEventListener('input',updateEstimate)
health()
if(ACTIVE_JOB){ $('#go').disabled=true; $('#log').style.display='block'; $('#cancel').style.display='inline-block'; $('#cancel').dataset.job=ACTIVE_JOB; poll(ACTIVE_JOB) }
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _authed(self):
        got = self.headers.get("X-Token") or ""
        if not TOKEN or not hmac.compare_digest(got, TOKEN):
            self._send(401, json.dumps({"error": "unauthorized"}))
            return False
        return True

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        relative = ("transfer.html" if TRANSFER_ONLY else "index.html") if path == "/" else path.lstrip("/")
        target = os.path.realpath(os.path.join(FRONTEND_DIR, relative))
        if target != FRONTEND_DIR and target.startswith(FRONTEND_DIR + os.sep) and os.path.isfile(target):
            with open(target, "rb") as f:
                content = f.read()
            ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            return self._send(200, content, ctype)
        self._send(404, json.dumps({"error": "not found"}))

    ROUTES = {
        "/api/parse": "handle_parse",
        "/api/bili/login/status": "handle_bili_login_status",
        "/api/bili/login/start": "handle_bili_login_start",
        "/api/bili/login/poll": "handle_bili_login_poll",
        "/api/bili/logout": "handle_bili_logout",
        "/api/bili/restart": "handle_bili_restart",
        "/api/pull": "handle_pull",
        "/api/torrent": "handle_torrent",
        "/api/torrent/inspect": "handle_torrent_inspect",
        "/api/drive/download": "handle_drive_download",
        "/api/remote/list": "handle_remote_list",
        "/api/remote/queue": "handle_remote_queue",
        "/api/status": "handle_status",
        "/api/log": "handle_log",
        "/api/cancel": "handle_cancel",
        "/api/pause": "handle_pause",
        "/api/resume": "handle_resume",
        "/api/health": "handle_health",
        "/api/jobs": "handle_jobs",
        "/api/retry": "handle_retry",
        "/api/process": "handle_process",
        "/api/upload": "handle_upload",
        "/api/probe": "handle_probe",
        "/api/files/downloads": "handle_list_downloads",
        "/api/files/drive": "handle_list_drive",
        "/api/delete/downloads": "handle_delete_downloads",
        "/api/delete/drive": "handle_delete_drive",
    }

    def do_POST(self):
        try:
            handler = self.ROUTES.get(self.path)
            if not handler:
                return self._send(404, json.dumps({"error": "not found"}))
            if not self._authed():
                return
            getattr(self, handler)(self._json_body())
        except Exception as e:  # noqa: BLE001 — surface any error as JSON to the UI
            self._send(500, json.dumps({"error": str(e)}))

    def handle_bili_login_status(self, body):
        del body
        self._send(200, json.dumps(bili_auth.status()))

    def handle_bili_login_start(self, body):
        del body
        self._send(200, json.dumps(bili_auth.start()))

    def handle_bili_login_poll(self, body):
        self._send(200, json.dumps(bili_auth.poll(str(body.get("key") or ""))))

    def handle_bili_logout(self, body):
        del body
        bili_auth.logout()
        self._send(200, json.dumps({"ok": True}))

    def handle_bili_restart(self, body):
        del body
        # Restarting Bili23 drops its in-flight downloads.
        if any(item["status"] in ("running", "paused") and PULL in item["command"] for item in jobs.values()):
            return self._send(409, json.dumps({"error": "มีงานดาวน์โหลด Bilibili กำลังทำอยู่ รอให้เสร็จก่อนรีสตาร์ต Bili23"}))
        bili_auth.restart_bili23()
        self._send(200, json.dumps({"ok": True}))

    def handle_parse(self, body):
        url = (body.get("url") or "").strip()
        if not url:
            return self._send(400, json.dumps({"error": "url required"}))
        env = {**os.environ, "BILI_PARSE_ONLY": "1"}
        r = subprocess.run(
            [sys.executable, BILI_PULL, url],
            env=env, capture_output=True, text=True, timeout=150,
        )
        if r.returncode != 0:
            return self._send(502, json.dumps({"error": (r.stderr or "parse failed").strip()[-800:]}))
        self._send(200, r.stdout.strip() or "[]")

    def handle_pull(self, body):
        url = (body.get("url") or "").strip()
        ids = body.get("episode_ids") or []
        if not url or not ids:
            return self._send(400, json.dumps({"error": "url and episode_ids required"}))
        job = secrets.token_hex(8)
        log_path = os.path.join(JOBS_DIR, job + ".log")
        state_path = os.path.join(JOBS_DIR, job + ".json")
        meta_path = os.path.join(JOBS_DIR, job + ".meta.json")
        env = {
            **os.environ,
            "BILI_EPISODE_IDS": json.dumps(ids),
            "BILI_JOB_STATE": state_path,
            "BILI_DOWNLOAD_ONLY": "1",
        }
        choices = {"quality": BILI_VIDEO_QUALITIES, "codec": BILI_VIDEO_CODECS,
                   "audio_quality": BILI_AUDIO_QUALITIES, "container": BILI_CONTAINERS}
        for name, allowed in choices.items():
            value = str(body.get(name) or "").strip()
            if value and value not in allowed:
                return self._send(400, json.dumps({"error": f"invalid {name}: {value}"}))
        q = (body.get("quality") or "").strip()
        if q:
            env["BILI_VIDEO_QUALITY"] = q
        # Default to H.264: Bilibili serves an AVC stream at every quality
        # here, 4K included, so prep-hls copies it instead of re-encoding HEVC.
        # (Trade-off: the AVC 4K stream is SDR, not HDR.) If a quality has no
        # AVC, Bili23 falls back to the next codec on its own. A transfer-only
        # download is not converted, so it may ask for HEVC/AV1 or "auto".
        env["BILI_VIDEO_CODEC"] = (body.get("codec") or "").strip() or "AVC/H.264"
        if a := (body.get("audio_quality") or "").strip():
            env["BILI_AUDIO_QUALITY"] = a
        if c := (body.get("container") or "").strip():
            env["BILI_CONTAINER"] = c
        if body.get("subtitle"):
            env["BILI_SUBTITLE"] = "1"
        if body.get("redownload"):
            env["BILI_REDOWNLOAD"] = "1"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({
                "job": job,
                "url": url,
                "episode_ids": ids,
                "quality": q or "4K",
                "codec": env["BILI_VIDEO_CODEC"],
                "audio_quality": env.get("BILI_AUDIO_QUALITY", ""),
                "container": env.get("BILI_CONTAINER", "mp4"),
                "subtitle": bool(body.get("subtitle")),
                "redownload": bool(body.get("redownload")),
                "kind": "download",
                "created": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            }, f, ensure_ascii=False, indent=2)
        overrides = {key: env[key] for key in (
            "BILI_EPISODE_IDS", "BILI_JOB_STATE", "BILI_DOWNLOAD_ONLY",
            "BILI_VIDEO_QUALITY", "BILI_VIDEO_CODEC", "BILI_REDOWNLOAD",
            "BILI_AUDIO_QUALITY", "BILI_CONTAINER", "BILI_SUBTITLE",
        ) if key in env}
        queue.submit(job, "download", ["bash", PULL, url], overrides)
        self._send(200, json.dumps({"job": job}))

    def handle_torrent_inspect(self, body):
        return self.handle_torrent({**body, "inspect": True})

    def handle_drive_download(self, body):
        source = str(body.get("source") or body.get("file_id") or "").strip()
        resource_key = str(body.get("resource_key") or "").strip()
        if len(source) > 4096:
            return self._send(400, json.dumps({"error": "Drive link is too long"}))
        if source.startswith("https://"):
            parsed = urlparse(source)
            if parsed.hostname != "drive.google.com" or parsed.username or parsed.password or parsed.port:
                return self._send(400, json.dumps({"error": "use a Google Drive file link or file ID"}))
            match = re.fullmatch(r"/file/d/([A-Za-z0-9_-]+)/?(?:view|edit)?/?", parsed.path)
            if match:
                file_id = match.group(1)
            elif parsed.path in ("/open", "/uc"):
                file_id = (parse_qs(parsed.query).get("id") or [""])[0]
            else:
                return self._send(400, json.dumps({"error": "only individual Drive files are supported, not folders or Google Docs"}))
            resource_key = (parse_qs(parsed.query).get("resourcekey") or [resource_key])[0]
        else:
            file_id = source
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,128}", file_id) or (resource_key and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", resource_key)):
            return self._send(400, json.dumps({"error": "invalid Drive file ID or resource key"}))
        job = secrets.token_hex(8)
        config_path = os.path.join(JOBS_DIR, job + ".config.json")
        state_path = os.path.join(JOBS_DIR, job + ".json")
        config = {"file_id": file_id, "resource_key": resource_key,
                  "destination": os.path.join(DOWNLOADS_DIR, "drive")}
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        with open(os.path.join(JOBS_DIR, job + ".meta.json"), "w", encoding="utf-8") as handle:
            json.dump({"job": job, "kind": "drive_download", "source": source, "resource_key": resource_key,
                       "created": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}, handle)
        queue.submit(job, "download", ["node", DRIVE_DOWNLOAD, config_path, state_path])
        self._send(200, json.dumps({"job": job}))

    def handle_torrent(self, body, retry_destination=None):
        inspecting = body.get("inspect") is True
        selected = body.get("selected_files")
        inspection_job = str(body.get("inspection_job") or "")
        if inspection_job:
            if not re.fullmatch(r"[a-f0-9]{16}", inspection_job):
                return self._send(400, json.dumps({"error": "invalid inspection job"}))
            with open(os.path.join(JOBS_DIR, inspection_job + ".json"), encoding="utf-8") as handle:
                listing = json.load(handle)
            valid = {entry["index"] for entry in listing.get("torrent_files", [])}
            if listing.get("phase") != "ready" or not isinstance(selected, list) or not selected or any(type(n) is not int or n not in valid for n in selected):
                return self._send(400, json.dumps({"error": "เลือกไฟล์อย่างน้อยหนึ่งไฟล์จากรายการ torrent"}))
            with open(os.path.join(JOBS_DIR, inspection_job + ".json.prepared.torrent"), "rb") as handle:
                body = {**body, "source": "", "torrent_data": base64.b64encode(handle.read()).decode("ascii")}
        elif selected is not None:
            # Retry uses the stored torrent, but still validates file indices.
            from torrent_download import torrent_files
            try:
                _, entries = torrent_files(base64.b64decode(body.get("torrent_data", ""), validate=True))
                valid = {entry["index"] for entry in entries}
                if not isinstance(selected, list) or not selected or any(type(n) is not int or n not in valid for n in selected):
                    raise ValueError("invalid file selection")
            except Exception:
                return self._send(400, json.dumps({"error": "invalid torrent file selection"}))
        source = str(body.get("source") or "").strip()
        encoded = str(body.get("torrent_data") or "").strip()
        supplied_name = str(body.get("name") or "").strip()
        if bool(source) == bool(encoded):
            return self._send(400, json.dumps({"error": "ใส่ magnet/URL หรือเลือกไฟล์ .torrent อย่างใดอย่างหนึ่ง"}))
        if source and not (source.startswith("magnet:?") or source.startswith("https://") or source.startswith("http://")):
            return self._send(400, json.dumps({"error": "รองรับเฉพาะ magnet link และ URL http(s) ของไฟล์ .torrent"}))
        if len(source) > 16384:
            return self._send(400, json.dumps({"error": "torrent URL ยาวเกินไป"}))

        job = secrets.token_hex(8)
        torrent_path = ""
        if encoded:
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                return self._send(400, json.dumps({"error": "ไฟล์ .torrent ไม่ถูกต้อง"}))
            if not payload or len(payload) > 4 * 1024 * 1024 or not payload.startswith(b"d"):
                return self._send(400, json.dumps({"error": "ไฟล์ .torrent ต้องเป็น bencode และมีขนาดไม่เกิน 4 MB"}))
            torrent_path = os.path.join(JOBS_DIR, job + ".torrent")
            with open(torrent_path, "wb") as handle:
                handle.write(payload)
            source_arg = torrent_path
        else:
            source_arg = source

        inferred = ""
        if source.startswith("magnet:?"):
            inferred = unquote((parse_qs(urlparse(source).query).get("dn") or [""])[0])
        elif source:
            inferred = os.path.basename(unquote(urlparse(source).path)).removesuffix(".torrent")
        elif supplied_name:
            inferred = supplied_name.removesuffix(".torrent")
        label = supplied_name or inferred or ("torrent-" + job[:8])
        label = re.sub(r"[^\w .()\[\]-]+", "_", os.path.basename(label), flags=re.UNICODE).strip(" .")[:100]
        if not label:
            label = "torrent-" + job[:8]
        destination = os.path.realpath(retry_destination) if retry_destination else os.path.realpath(os.path.join(TORRENT_DOWNLOADS_DIR, f"{label}-{job[:6]}"))
        if not destination.startswith(TORRENT_DOWNLOADS_DIR + os.sep):
            return self._send(400, json.dumps({"error": "invalid torrent destination"}))

        log_path = os.path.join(JOBS_DIR, job + ".log")
        state_path = os.path.join(JOBS_DIR, job + ".json")
        meta_path = os.path.join(JOBS_DIR, job + ".meta.json")
        created = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        state = {"kind": "torrent_inspect" if inspecting else "torrent", "phase": "queued", "status": "queued", "progress": 0, "destination": destination}
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        meta = {
            "job": job, "kind": "torrent_inspect" if inspecting else "torrent", "created": created, "name": label, "selected_files": selected,
            "source": source, "torrent_file": torrent_path, "destination": destination,
        }
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2)
        command = [sys.executable, TORRENT_DOWNLOAD, source_arg, destination, state_path]
        if inspecting:
            command.insert(2, "--inspect")
        elif selected:
            command.append(",".join(map(str, sorted(set(selected)))))
        queue.submit(job, "inspect" if inspecting else "download", command)
        self._send(200, json.dumps({"job": job}))

    def handle_process(self, body):
        if TRANSFER_ONLY:
            return self._send(403, json.dumps({"error": "HLS conversion is disabled in transfer-only mode"}))
        try:
            job = enqueue_process(body)
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)}))
        self._send(200, json.dumps({"job": job}))

    # ---- Drive queue (rclone remote) ----

    def handle_remote_list(self, body):
        if not RCLONE_REMOTE:
            return self._send(400, json.dumps({"error": "ต้องตั้ง BILI_RCLONE_REMOTE (เช่น metube:tube) ก่อนจึงจะเปิดดูไฟล์บน Drive ได้"}))
        try:
            relative = remote_relative(body.get("path"))
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)}))
        result = subprocess.run(["rclone", "lsjson", remote_spec(relative), "--no-mimetype"],
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            return self._send(502, json.dumps({"error": (result.stderr or "rclone lsjson failed").strip()[-500:]}))
        items = []
        for entry in json.loads(result.stdout or "[]"):
            path = f"{relative}/{entry['Name']}" if relative else entry["Name"]
            items.append({
                "name": entry["Name"], "path": path, "dir": bool(entry.get("IsDir")),
                "size": entry.get("Size", 0) if not entry.get("IsDir") else None,
                "modified": entry.get("ModTime", ""),
                "video": not entry.get("IsDir") and entry["Name"].lower().endswith(VIDEO_EXTENSIONS),
            })
        items.sort(key=lambda item: (not item["dir"], item["name"].lower()))
        self._send(200, json.dumps({"remote": RCLONE_REMOTE, "path": relative, "items": items}))

    def handle_remote_queue(self, body):
        if not RCLONE_REMOTE:
            return self._send(400, json.dumps({"error": "BILI_RCLONE_REMOTE is not configured"}))
        try:
            paths = [remote_relative(path) for path in (body.get("paths") or [])]
        except ValueError as exc:
            return self._send(400, json.dumps({"error": str(exc)}))
        if not paths or any(not path.lower().endswith(VIDEO_EXTENSIONS) for path in paths):
            return self._send(400, json.dumps({"error": "เลือกไฟล์วิดีโออย่างน้อยหนึ่งไฟล์"}))
        hls = body.get("hls")
        if hls is not None:
            if TRANSFER_ONLY:
                return self._send(403, json.dumps({"error": "HLS conversion is disabled in transfer-only mode"}))
            if not isinstance(hls, dict):
                return self._send(400, json.dumps({"error": "invalid HLS profile"}))
            hls = {key: hls[key] for key in HLS_OPTION_KEYS if key in hls}
        delete_remote = bool(body.get("delete_remote_source")) and hls is not None
        jobs_created = [enqueue_remote_download(path, {"hls": hls, "delete_remote_source": delete_remote}) for path in paths]
        self._send(200, json.dumps({"jobs": jobs_created}))

    def handle_upload(self, body):
        # Original-file transfers use the same independent upload lane as HLS.
        files = [str(item) for item in (body.get("files") or []) if item]
        if not files:
            return self._send(400, json.dumps({"error": "no files selected"}))
        for path in files:
            resolved = os.path.realpath(path)
            if not resolved.startswith(DOWNLOADS_DIR + os.sep) or not os.path.isfile(resolved):
                return self._send(400, json.dumps({"error": f"invalid downloaded file: {path}"}))
        job = enqueue_upload([os.path.realpath(path) for path in files], "source")
        self._send(200, json.dumps({"job": job}))

    def handle_log(self, body):
        """Read a job log by byte range so the viewer can follow it incrementally.

        With no offset it returns the tail; with `offset` it returns what was
        appended since; with `before` it returns the chunk preceding that byte
        (to page back through older lines); `full` returns the whole log for
        download. Ranges always cover whole lines, so a multi-byte character is
        never split between two reads.
        """
        job = body.get("job") or ""
        if len(job) != 16 or any(c not in "0123456789abcdef" for c in job):
            return self._send(404, json.dumps({"error": "unknown job"}))
        log_path = os.path.join(JOBS_DIR, job + ".log")
        try:
            size = os.path.getsize(log_path)
        except OSError:
            return self._send(404, json.dumps({"error": "unknown job"}))
        offset, before = body.get("offset"), body.get("before")
        valid = lambda value: isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= size
        reset = offset is not None and not valid(offset)
        if body.get("full"):
            start, end, trim_head = max(0, size - LOG_FULL_LIMIT), size, True
        elif before is not None:
            end = before if valid(before) else size
            start, trim_head = max(0, end - LOG_CHUNK), True
        elif offset is not None and not reset:
            start, end, trim_head = offset, min(size, offset + LOG_CHUNK), False
        else:
            start, end, trim_head = max(0, size - LOG_TAIL), size, True
        with open(log_path, "rb") as source:
            source.seek(start)
            data = source.read(end - start)
        if trim_head and start > 0:
            cut = data.find(b"\n")
            if cut >= 0:
                data, start = data[cut + 1:], start + cut + 1
        last = data.rfind(b"\n")
        if last >= 0:
            data = data[:last + 1]
        elif len(data) < LOG_CHUNK:
            # An unfinished line: wait until the worker completes it.
            data = b""
        self._send(200, json.dumps({
            "text": data.decode("utf-8", "replace"),
            "start": start, "offset": start + len(data), "size": size, "reset": reset,
        }))

    def handle_status(self, body):
        job = body.get("job") or ""
        j = jobs.get(job)
        if not j:
            if len(job) != 16 or any(c not in "0123456789abcdef" for c in job):
                return self._send(404, json.dumps({"error": "unknown job"}))
            log_path = os.path.join(JOBS_DIR, job + ".log")
            if not os.path.isfile(log_path):
                return self._send(404, json.dumps({"error": "unknown job"}))
            with open(log_path, errors="replace") as f:
                text = f.read()[-8000:]
            cancelled = "CANCELLED BY USER" in text
            state = {}
            try:
                with open(os.path.join(JOBS_DIR, job + ".json"), encoding="utf-8") as f:
                    state = json.load(f)
            except (OSError, ValueError):
                pass
            return self._send(200, json.dumps({
                "running": False, "log": text, "cancelled": cancelled,
                "exit_code": None, "state": state,
            }))
        running = j["status"] in ("queued", "running", "paused")
        try:
            with open(j["log"], errors="replace") as f:
                text = f.read()[-8000:]
        except OSError:
            text = ""
        state = {}
        try:
            with open(j["state"], encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            pass
        if j["status"] in ("queued", "paused", "interrupted", "cancelled"):
            state["status"] = j["status"]
            state["phase"] = j["status"]
        state["lane"] = j["lane"]
        self._send(200, json.dumps({
            "running": running,
            "log": text,
            "cancelled": j.get("cancelled", False),
            "exit_code": j["proc"].poll() if j["proc"] is not None else None,
            "state": state,
        }))

    def _bili_tasks(self, item, action):
        try:
            with open(item["meta"], encoding="utf-8") as source:
                meta = json.load(source)
            if meta.get("kind") != "download":
                return
            with open(item["state"], encoding="utf-8") as source:
                task_ids = json.load(source).get("task_ids") or []
        except (OSError, ValueError):
            task_ids = []
        if not task_ids:
            if action == "cancel":
                return
            raise RuntimeError("Bili23 task IDs are not ready yet; try again shortly")
        result = subprocess.run(
            [sys.executable, BILI_PULL, f"--{action}", *task_ids],
            env=os.environ, capture_output=True, text=True, timeout=45,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or f"Bili23 {action} failed").strip()[-800:])

    def handle_pause(self, body):
        job = str(body.get("job") or "")
        if job not in jobs:
            return self._send(404, json.dumps({"error": "unknown job"}))
        try:
            item = queue.pause(job, before_pause=lambda item: self._bili_tasks(item, "pause"))
        except (ValueError, RuntimeError) as exc:
            return self._send(409, json.dumps({"error": str(exc)}))
        self._send(200, json.dumps({"job": job, "status": item["status"]}))

    def handle_resume(self, body):
        job = str(body.get("job") or "")
        if job not in jobs:
            return self._send(404, json.dumps({"error": "unknown job"}))
        try:
            item = queue.resume(job, before_resume=lambda item: self._bili_tasks(item, "resume"))
        except (ValueError, RuntimeError) as exc:
            return self._send(409, json.dumps({"error": str(exc)}))
        self._send(200, json.dumps({"job": job, "status": item["status"]}))

    def handle_cancel(self, body):
        job = body.get("job") or ""
        if job not in jobs:
            return self._send(404, json.dumps({"error": "unknown job"}))
        def cancel_external(item):
            try:
                self._bili_tasks(item, "cancel")
            except RuntimeError as exc:
                with open(item["log"], "a", encoding="utf-8") as out:
                    out.write(f"Bili23 cancellation warning: {exc}\n")
        try:
            item = queue.cancel(job, before_cancel=cancel_external)
        except (ValueError, RuntimeError) as exc:
            return self._send(409, json.dumps({"error": str(exc)}))
        with open(item["log"], errors="replace") as f:
            text = f.read()[-8000:]
        self._send(200, json.dumps({"cancelled": True, "log": text}))

    def handle_health(self, body):
        del body
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "bili23.service"],
                capture_output=True, text=True, timeout=5,
            )
            bili23 = (result.stdout or "unknown").strip()
        except (OSError, subprocess.TimeoutExpired):
            bili23 = "unknown"
        usage = shutil.disk_usage(DOWNLOADS_DIR)
        active = sum(1 for item in jobs.values() if item["status"] == "running")
        self._send(200, json.dumps({
            "bili23": bili23,
            "disk_total": usage.total,
            "disk_free": usage.free,
            "active_jobs": active,
            "convert_gpus": queue.convert_gpus,
            "transfer_only": TRANSFER_ONLY,
        }))

    def handle_probe(self, body):
        paths = [str(item) for item in (body.get("paths") or []) if item]
        if not paths or len(paths) > 10:
            return self._send(400, json.dumps({"error": "select between 1 and 10 files"}))
        output = []
        for supplied in paths:
            path = os.path.realpath(supplied)
            if not path.startswith(DOWNLOADS_DIR + os.sep) or not os.path.isfile(path):
                return self._send(400, json.dumps({"error": f"invalid downloaded file: {supplied}"}))
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return self._send(422, json.dumps({"error": (result.stderr or "ffprobe failed").strip()[-800:]}))
            info = json.loads(result.stdout)
            streams = info.get("streams") or []
            fmt = info.get("format") or {}
            video_stream = next((item for item in streams if item.get("codec_type") == "video"), {})
            audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
            subtitles = [item for item in streams if item.get("codec_type") == "subtitle"]

            def number(value):
                try:
                    return float(value or 0)
                except (TypeError, ValueError):
                    return 0

            def first_packet(selector):
                packet = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", selector,
                     "-read_intervals", "%+#1", "-show_packets", "-show_entries",
                     "packet=pts_time", "-of", "default=nw=1:nk=1", path],
                    capture_output=True, text=True, timeout=15,
                )
                return number((packet.stdout or "0").splitlines()[0] if packet.stdout else 0)

            side_data = video_stream.get("side_data_list") or []
            transfer = video_stream.get("color_transfer") or ""
            output.append({
                "path": path, "name": os.path.basename(path),
                "size": int(number(fmt.get("size"))),
                "duration": number(fmt.get("duration")),
                "bit_rate": int(number(fmt.get("bit_rate"))),
                "video": {
                    "codec": video_stream.get("codec_name"), "profile": video_stream.get("profile"),
                    "width": video_stream.get("width"), "height": video_stream.get("height"),
                    "pix_fmt": video_stream.get("pix_fmt"),
                    "fps": video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"),
                    "bit_rate": int(number(video_stream.get("bit_rate"))),
                    "hdr": transfer in ("smpte2084", "arib-std-b67"),
                    "dolby_vision": any("dovi" in str(x).lower() or "dolby vision" in str(x).lower() for x in side_data),
                    "first_packet": first_packet("v:0"),
                },
                "audio": [{
                    "index": index + 1, "codec": stream.get("codec_name"), "profile": stream.get("profile"),
                    "channels": stream.get("channels"), "layout": stream.get("channel_layout"),
                    "sample_rate": int(number(stream.get("sample_rate"))),
                    "bit_rate": int(number(stream.get("bit_rate"))),
                    "language": (stream.get("tags") or {}).get("language", "und"),
                    "title": (stream.get("tags") or {}).get("title") or (stream.get("tags") or {}).get("name", ""),
                    "first_packet": first_packet(f"a:{index}"),
                } for index, stream in enumerate(audio_streams)],
                "subtitles": [{
                    "codec": stream.get("codec_name"),
                    "language": (stream.get("tags") or {}).get("language", "und"),
                    "title": (stream.get("tags") or {}).get("title", ""),
                } for stream in subtitles],
            })
        self._send(200, json.dumps(output))

    def handle_jobs(self, body):
        del body
        rows = []
        for name in os.listdir(JOBS_DIR):
            if not name.endswith(".log"):
                continue
            job = name[:-4]
            log_path = os.path.join(JOBS_DIR, name)
            meta_path = os.path.join(JOBS_DIR, job + ".meta.json")
            meta = {}
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                pass
            current = jobs.get(job)
            running = bool(current and current["status"] == "running")
            try:
                with open(log_path, errors="replace") as f:
                    tail = f.read()[-4000:]
                size = os.path.getsize(log_path)
                modified = os.path.getmtime(log_path)
            except OSError:
                continue
            state = {}
            try:
                with open(os.path.join(JOBS_DIR, job + ".json"), encoding="utf-8") as f:
                    state = json.load(f)
            except (OSError, ValueError):
                pass
            if current:
                status = current["status"]
            elif state.get("status") in ("downloaded", "completed", "failed", "cancelled"):
                status = state["status"]
            elif "CANCELLED BY USER" in tail:
                status = "cancelled"
            elif "all done" in tail:
                status = "completed"
            elif "failed" in tail.lower() or "nothing downloaded" in tail.lower():
                status = "failed"
            else:
                status = "stopped"
            rows.append({
                "job": job, "status": status,
                "url": meta.get("url") or meta.get("source") or meta.get("name", ""),
                "kind": meta.get("kind", "download"),
                "lane": current["lane"] if current else meta.get("kind", "download"),
                "progress": state.get("progress", 0),
                "gpu": current.get("gpu") if current else state.get("gpu"),
                "parent_job": meta.get("parent_job"),
                "created": meta.get("created", time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(modified))),
                "log_size": size, "modified": modified,
                "started_at": (current or {}).get("started_at") or state.get("started_at"),
                "finished_at": (current or {}).get("finished_at") or state.get("finished_at"),
                "details": {key: state[key] for key in JOB_DETAIL_KEYS if state.get(key) not in (None, "")},
            })
        rows.sort(key=lambda row: (row["status"] in ("running", "queued", "paused"), row["modified"]), reverse=True)
        self._send(200, json.dumps(rows[:100]))

    def handle_retry(self, body):
        job = body.get("job") or ""
        if len(job) != 16 or any(c not in "0123456789abcdef" for c in job):
            return self._send(400, json.dumps({"error": "invalid job"}))
        meta_path = os.path.join(JOBS_DIR, job + ".meta.json")
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return self._send(404, json.dumps({"error": "this older job has no retry metadata"}))
        if meta.get("kind") == "torrent":
            original = jobs.get(job)
            if original and original["status"] in ("queued", "running", "paused"):
                return self._send(409, json.dumps({"error": "งาน torrent เดิมยังทำงานอยู่"}))
            destination = os.path.realpath(meta.get("destination") or "")
            if not destination.startswith(TORRENT_DOWNLOADS_DIR + os.sep):
                return self._send(400, json.dumps({"error": "invalid retry destination"}))
            if any(item["lane"] == "download" and item["status"] in ("queued", "running", "paused")
                   and len(item["command"]) > 3 and item["command"][1] == TORRENT_DOWNLOAD
                   and os.path.realpath(item["command"][3]) == destination for item in jobs.values()):
                return self._send(409, json.dumps({"error": "มีงานดาวน์โหลดลงโฟลเดอร์นี้อยู่แล้ว"}))
            if meta.get("source"):
                return self.handle_torrent({"source": meta["source"], "name": meta.get("name", "")}, retry_destination=destination)
            torrent_file = meta.get("torrent_file") or ""
            if os.path.isfile(torrent_file) and os.path.realpath(torrent_file).startswith(os.path.realpath(JOBS_DIR) + os.sep):
                with open(torrent_file, "rb") as handle:
                    encoded = base64.b64encode(handle.read()).decode("ascii")
                retry_body = {"torrent_data": encoded, "name": meta.get("name", "")}
                if meta.get("selected_files"):
                    retry_body["selected_files"] = meta["selected_files"]
                return self.handle_torrent(retry_body, retry_destination=destination)
            return self._send(404, json.dumps({"error": "ไม่พบไฟล์ .torrent เดิมสำหรับ retry"}))
        if meta.get("kind") == "hls":
            if TRANSFER_ONLY:
                return self._send(403, json.dumps({"error": "HLS conversion is disabled in transfer-only mode"}))
            try:
                new_job = enqueue_process(meta, pipeline=meta.get("pipeline"))
            except ValueError as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps({"job": new_job}))
        if meta.get("kind") == "remote_download":
            if not RCLONE_REMOTE:
                return self._send(400, json.dumps({"error": "BILI_RCLONE_REMOTE is not configured"}))
            return self._send(200, json.dumps({"job": enqueue_remote_download(meta.get("source") or "", meta.get("pipeline") or {})}))
        if meta.get("kind") == "drive_download":
            return self.handle_drive_download({"source": meta.get("source"), "resource_key": meta.get("resource_key")})
        if meta.get("kind") == "upload":
            try:
                new_job = enqueue_upload(meta.get("paths") or [], meta.get("upload_kind") or "source",
                                         meta.get("keep_local", False), meta.get("parent_job"))
            except (ValueError, OSError) as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps({"job": new_job}))
        return self.handle_pull(meta)

    # ---- Files tab ----

    def handle_list_downloads(self, body):
        out = []
        for root, _dirs, names in os.walk(DOWNLOADS_DIR):
            for n in names:
                p = os.path.join(root, n)
                if n.endswith((".aria2", ".partial")) or os.path.exists(p + ".aria2"):
                    continue
                if root.startswith(os.path.join(DOWNLOADS_DIR, "drive") + os.sep) and n.endswith(".part"):
                    continue
                try:
                    info = os.stat(p)
                    out.append({"path": p, "relative": os.path.relpath(p, DOWNLOADS_DIR),
                                "size": info.st_size, "modified": info.st_mtime})
                except OSError:
                    continue
        out.sort(key=lambda f: f["size"], reverse=True)
        self._send(200, json.dumps(out))

    def handle_delete_downloads(self, body):
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths or len(paths) > 1000:
            return self._send(400, json.dumps({"error": "select 1–1000 downloaded files"}))
        deleted = failed = 0
        with queue.lock:
            protected = []
            for item in jobs.values():
                if item["status"] not in ("queued", "running", "paused"):
                    continue
                if item["lane"] == "download" and len(item["command"]) > 3 and item["command"][1] == TORRENT_DOWNLOAD:
                    protected.append(("directory", os.path.realpath(item["command"][3])))
                try:
                    with open(item["meta"], encoding="utf-8") as handle:
                        meta = json.load(handle)
                except (OSError, ValueError):
                    continue
                for path in (meta.get("files") or []) + (meta.get("paths") or []):
                    protected.append(("file", os.path.realpath(path)))
                if item["lane"] == "download" and meta.get("destination"):
                    protected.append(("directory", os.path.realpath(meta["destination"])))
            for p in dict.fromkeys(path for path in paths if isinstance(path, str)):
                rp = os.path.realpath(p)
                if (not os.path.isabs(p) or not rp.startswith(DOWNLOADS_DIR + os.sep)
                        or os.path.islink(p) or not os.path.isfile(p) or p.endswith((".aria2", ".part"))
                        or os.path.exists(p + ".aria2")
                        or any(rp == target if kind == "file" else rp.startswith(target + os.sep)
                               for kind, target in protected)):
                    failed += 1
                    continue
                try:
                    os.remove(p)
                    deleted += 1
                except OSError:
                    failed += 1
            failed += sum(not isinstance(path, str) for path in paths)
        self._send(200, json.dumps({"deleted": deleted, "failed": failed}))

    def handle_list_drive(self, body):
        r = subprocess.run(
            ["node", DRIVE_FILES, "list"],
            env={**os.environ}, capture_output=True, text=True, timeout=90,
        )
        if r.returncode != 0:
            return self._send(502, json.dumps({"error": (r.stderr or "drive list failed").strip()[-800:]}))
        self._send(200, r.stdout.strip() or "[]")

    def handle_delete_drive(self, body):
        ids = [str(i) for i in (body.get("ids") or []) if i]
        if not ids:
            return self._send(400, json.dumps({"error": "no ids"}))
        r = subprocess.run(
            ["node", DRIVE_FILES, "delete", *ids],
            env={**os.environ}, capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            return self._send(502, json.dumps({"error": (r.stderr or "drive delete failed").strip()[-800:]}))
        res = json.loads(r.stdout or "{}")
        self._send(200, json.dumps({"deleted": len(res.get("deleted", [])), "failed": len(res.get("failed", []))}))


def main():
    if not TOKEN:
        sys.exit("set BILI_WEB_TOKEN")
    srv = ThreadingHTTPServer((HOST, PORT), H)
    threading.Thread(target=queue.run, name="bili-job-queue", daemon=True).start()
    print(f"bili-web on {HOST}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
