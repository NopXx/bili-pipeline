#!/usr/bin/env python3
"""Write the schemaVersion 2 <slug>.info.json beside an HLS bundle.

prep-hls.sh is bash+ffmpeg by design, so it cannot serialise the nested manifest
its PowerShell twin (prep-hls.ps1) produces. This helper is the twin's equal:
prep-hls.sh records the few decisions only it knows (which rungs are raw vs
re-encoded, their target bitrates) as TSV, and this script re-probes the source
and scans the output folder to build everything else — matching prep-hls.ps1's
shape field-for-field so the library reads a bundle the same way whichever
converter made it. If python3 is missing, prep-hls.sh falls back to the tiny
legacy sidecar, so this stays an enhancement, never a hard dependency.

  write-info-json.py <input> <outdir> <slug>

Reads TSV record files and decision hints from the environment (INFO_*).
"""
import json
import os
import re
import subprocess
import sys

TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}


def env_flag(name):
    return os.environ.get(name, "") == "1"


def probe(path):
    raw = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        text=True, errors="replace",
    )
    return json.loads(raw)


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def bit_depth(pix_fmt):
    m = re.search(r"(\d+)(?:le|be)$", pix_fmt or "")
    return int(m.group(1)) if m else 8


def src_range(transfer):
    return {"smpte2084": "HDR10", "arib-std-b67": "HLG"}.get(transfer or "", "SDR")


def hdr_label(transfer, has_dovi):
    rng = src_range(transfer)
    if rng == "SDR":
        return None
    if rng == "HDR10" and has_dovi:
        return "Dolby Vision"
    return rng


def quality_label(w, h):
    w = w or 0
    h = h or 0
    if w >= 3800 or h >= 2160:
        return "4K"
    if h >= 1440:
        return "1440p"
    if h >= 1080:
        return "1080p"
    if h >= 720:
        return "720p"
    if h:
        return f"{h}p"
    return "unknown"


def channel_label(n):
    return {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(n, f"{n}ch")


def codec_string(name, profile=""):
    profile = profile or ""
    if name == "aac":
        return "mp4a.40.2"
    if name == "flac":
        return "fLaC"
    if name == "eac3":
        return "ec-3"
    if name == "ac3":
        return "ac-3"
    if name == "dts":
        if re.search(r"master", profile, re.I):
            return "dtsl"
        if re.search(r"high resolution", profile, re.I):
            return "dtsh"
        return "dtsc"
    return None


def scaled_width(src_w, src_h, h):
    if not h or not src_h or not src_w:
        return src_w or 0
    return int(round(src_w * h / src_h / 2.0) * 2)


def read_records(path):
    rows = []
    if not path or not os.path.isfile(path):
        return rows
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if line:
                rows.append(line.split("\t"))
    return rows


def stream_title(tags):
    return tags.get("title") or tags.get("name") or ""


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: write-info-json.py <input> <outdir> <slug>")
    src, outdir, slug = sys.argv[1:]
    info = probe(src)
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    if not video_streams:
        raise SystemExit("no video stream to describe")
    v0 = video_streams[0]

    copy_video = env_flag("INFO_COPY_VIDEO")
    preserve_hdr = env_flag("INFO_PRESERVE_HDR")
    has_dovi = env_flag("INFO_HAS_DOVI")
    copied_source_video = env_flag("INFO_COPIED_SOURCE_VIDEO")
    out_codec = os.environ.get("INFO_OUT_CODEC") or v0.get("codec_name") or "h264"
    video_target = os.environ.get("INFO_VIDEO_TARGET_BITRATE") or None

    src_w = to_int(v0.get("width")) or 0
    src_h = to_int(v0.get("height")) or 0
    pix_fmt = v0.get("pix_fmt") or ""
    transfer = v0.get("color_transfer") or ""
    depth = bit_depth(pix_fmt)
    src_hdr = hdr_label(transfer, has_dovi)

    # Legacy badges the current backend already reads, plus the richer set.
    kept_hdr = src_hdr if (copied_source_video or preserve_hdr) else None
    audio_atmos = any(
        re.search(r"atmos", stream_title(s.get("tags", {})), re.I)
        or re.search(r"atmos", s.get("profile") or "", re.I)
        for s in audio_streams
    )

    # ------------------------------------------------------------------ video
    fps = None
    m = re.match(r"^(\d+)/(\d+)$", v0.get("avg_frame_rate") or "")
    if m and int(m.group(2)):
        fps = round(int(m.group(1)) / int(m.group(2)), 3)

    video_block = {
        "codec": v0.get("codec_name"),
        "outputCodec": out_codec,
        "profile": v0.get("profile"),
        "width": src_w,
        "height": src_h,
        "quality": quality_label(src_w, src_h),
        "pixelFormat": pix_fmt,
        "bitDepth": depth,
        "frameRate": fps,
        "bitrate": to_int(v0.get("bit_rate")),
        "colorRange": v0.get("color_range"),
        "colorSpace": v0.get("color_space"),
        "colorTransfer": transfer,
        "colorPrimaries": v0.get("color_primaries"),
        "hdr": kept_hdr,
        "dolbyVision": bool(copied_source_video and has_dovi),
        "preserveHdr": bool(copy_video or preserve_hdr),
        "targetBitrate": None if copy_video else video_target,
    }

    # ------------------------------------------------------------ audio tracks
    audio_tracks = []
    for s in audio_streams:
        tags = s.get("tags", {})
        title = stream_title(tags)
        disp = s.get("disposition", {})
        audio_tracks.append({
            "index": to_int(s.get("index")),
            "codec": s.get("codec_name"),
            "profile": s.get("profile"),
            "language": tags.get("language") or "und",
            "title": title,
            "channels": to_int(s.get("channels")),
            "layout": s.get("channel_layout"),
            "sampleRate": to_int(s.get("sample_rate")),
            "bitrate": to_int(s.get("bit_rate")),
            "default": bool(disp.get("default")),
            "forced": bool(disp.get("forced")),
            "atmos": bool(re.search(r"atmos", title, re.I) or re.search(r"atmos", s.get("profile") or "", re.I)),
            "commentary": bool(re.search(r"commentary", title, re.I) or disp.get("comment")),
        })

    # --------------------------------------------------------- subtitle tracks
    subtitle_tracks = []
    for s in sub_streams:
        tags = s.get("tags", {})
        title = stream_title(tags)
        disp = s.get("disposition", {})
        is_text = s.get("codec_name") in TEXT_SUBS
        subtitle_tracks.append({
            "index": to_int(s.get("index")),
            "codec": s.get("codec_name"),
            "language": tags.get("language") or "und",
            "title": title,
            "kind": "text" if is_text else "image",
            "included": is_text,
            "default": bool(disp.get("default")),
            "forced": bool(disp.get("forced")),
            "closedCaptions": bool(re.search(r"\bCC\b|closed captions", title, re.I)),
            "sdh": bool(re.search(r"\bSDH\b|hearing impaired", title, re.I) or disp.get("hearing_impaired")),
        })

    # ------------------------------------------------------- video renditions
    video_source_codec = v0.get("codec_name")
    video_renditions = []
    for row in read_records(os.environ.get("INFO_VID_REC")):
        index, raw_s, codec, height_arg, br = (row + ["", "", "", "", ""])[:5]
        raw = raw_s == "1"
        rh = to_int(height_arg) or src_h
        rw = src_w if raw else scaled_width(src_w, src_h, rh)
        keeps = raw or preserve_hdr
        video_renditions.append({
            "file": f"{slug}.part{index}",
            "playlist": f"{slug}.part{index}.m3u8",
            "mediaFile": f"{slug}.part{index}.m4s",
            "kind": "video",
            "raw": raw,
            "source": "copied untouched" if raw else "re-encoded",
            "codec": codec,
            "sourceCodec": video_source_codec,
            "width": rw,
            "height": rh,
            "resolution": f"{rw}x{rh}" if rw and rh else None,
            "quality": quality_label(rw, rh),
            "hdr": src_hdr if keeps else None,
            "dolbyVision": bool(raw and has_dovi),
            "bitDepth": depth if keeps else 8,
            "targetBitrate": br or None,
        })

    # ------------------------------------------------------- audio renditions
    by_src_index = {to_int(s.get("index")): s for s in audio_streams}
    # a:N in ffmpeg is the Nth audio stream in order; map audio ordinal -> stream.
    audio_by_ordinal = audio_streams
    audio_renditions = []
    for row in read_records(os.environ.get("INFO_AUD_REC")):
        cols = (row + [""] * 11)[:11]
        out_index, name, group, src_index, spec, raw_s, padded_s, reenc_s, out_c, chans, kbps = cols
        ordinal = to_int(src_index) or 0
        stream = audio_by_ordinal[ordinal] if ordinal < len(audio_by_ordinal) else {}
        tags = stream.get("tags", {})
        title = stream_title(tags) or (tags.get("language") or "und")
        profile = stream.get("profile") or ""
        raw = raw_s == "1"
        padded = padded_s == "1"
        reencoded = reenc_s == "1"
        actual_channels = to_int(chans)
        if reencoded:
            source = "re-encoded"
        elif padded:
            source = "copied with silent preroll"
        else:
            source = "copied untouched"
        audio_renditions.append({
            "file": f"{slug}.part{name}",
            "playlist": f"{slug}.part{name}.m3u8",
            "mediaFile": f"{slug}.part{name}.m4s",
            "kind": "audio",
            "group": group,
            "raw": raw,
            "padded": padded,
            "source": source,
            "codec": out_c,
            "codecString": codec_string(out_c, profile),
            "sourceCodec": stream.get("codec_name"),
            "language": tags.get("language") or "und",
            "title": title,
            "channels": actual_channels,
            "channelLayout": stream.get("channel_layout") if spec == "raw" else channel_label(actual_channels),
            "bitrateKbps": to_int(kbps),
            "atmos": bool(re.search(r"atmos", title, re.I) or re.search(r"atmos", profile, re.I)),
            "default": out_index == "0",
        })

    # ---------------------------------------------------- subtitle renditions
    subtitle_renditions = []
    for i, s in enumerate(sub_streams):
        if s.get("codec_name") not in TEXT_SUBS:
            continue
        tags = s.get("tags", {})
        language = tags.get("language") or "und"
        candidate = f"{slug}.sub-{language}-{i}.vtt"
        if not os.path.isfile(os.path.join(outdir, candidate)):
            continue
        disp = s.get("disposition", {})
        subtitle_renditions.append({
            "file": candidate,
            "kind": "subtitle",
            "format": "webvtt",
            "sourceCodec": s.get("codec_name"),
            "language": language,
            "title": stream_title(tags),
            "forced": bool(disp.get("forced")),
            "default": bool(disp.get("default")),
        })

    # --------------------------------------------------------- file manifest
    rendition_by_base = {}
    for r in video_renditions + audio_renditions:
        rendition_by_base[r["file"]] = r

    esc = re.escape(slug)
    sub_re = re.compile(rf"^{esc}\.sub-(?P<lang>[^.]+)-\d+\.vtt$")
    part_re = re.compile(rf"^(?P<base>{esc}\.part.+?)(?P<init>-init)?\.(?P<ext>m3u8|m4s|mp4)$")
    info_name = f"{slug}.info.json"

    manifest_files = []
    for name in sorted(os.listdir(outdir)):
        full = os.path.join(outdir, name)
        if not os.path.isfile(full) or name == info_name:
            continue
        entry = {"name": name, "sizeBytes": os.path.getsize(full)}
        if name == f"{slug}.m3u8":
            entry["role"] = "master playlist"
        elif name == f"{slug}.poster.jpg":
            entry["role"] = "poster image"
        elif sub_re.match(name):
            entry["role"] = "subtitle"
            entry["format"] = "webvtt"
            entry["language"] = sub_re.match(name).group("lang")
        elif part_re.match(name):
            m = part_re.match(name)
            base = m.group("base")
            if m.group("ext") == "m3u8":
                entry["role"] = "media playlist"
            elif m.group("init"):
                entry["role"] = "fmp4 init segment"
            else:
                entry["role"] = "media segments (fmp4, single file)"
            entry["rendition"] = base
            r = rendition_by_base.get(base)
            if r:
                entry["streamKind"] = r["kind"]
                entry["codec"] = r["codec"]
                entry["raw"] = r["raw"]
                if r["kind"] == "video":
                    entry["resolution"] = r["resolution"]
                    if r["hdr"]:
                        entry["hdr"] = r["hdr"]
                else:
                    entry["language"] = r["language"]
                    entry["channels"] = r["channels"]
        else:
            entry["role"] = "other"
        manifest_files.append(entry)
    manifest_files.append({"name": info_name, "role": "manifest (this file)"})

    # -------------------------------------------------------------- assemble
    max_ch = max([r["channels"] or 0 for r in audio_renditions], default=0)
    audio_badge = "Dolby Atmos" if audio_atmos else "7.1" if max_ch >= 8 else "5.1" if max_ch >= 6 else None
    quality = quality_label(src_w, src_h)

    duration = to_float(fmt.get("duration"))
    manifest = {
        "schemaVersion": 2,
        "hdr": kept_hdr,
        "audio": audio_badge,
        "badges": {
            "quality": quality,
            "hdr": kept_hdr,
            "audio": audio_badge,
            "videoCodec": out_codec.upper(),
        },
        "source": {
            "fileName": os.path.basename(src),
            "sizeBytes": to_int(fmt.get("size")),
            "container": fmt.get("format_name"),
            "durationMs": int(duration * 1000) if duration else None,
            "bitrate": to_int(fmt.get("bit_rate")),
        },
        "video": video_block,
        "audioTracks": audio_tracks,
        "subtitleTracks": subtitle_tracks,
        "renditions": {
            "video": video_renditions,
            "audio": audio_renditions,
            "subtitles": subtitle_renditions,
        },
        "files": manifest_files,
    }

    out_path = os.path.join(outdir, info_name)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out_path)
    print(f"  info.json: schemaVersion 2 manifest ({len(manifest_files)} files)")


if __name__ == "__main__":
    main()
