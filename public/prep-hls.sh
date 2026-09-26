#!/usr/bin/env bash
#
# Turns an MKV with several audio tracks and subtitles into something a browser
# can actually play, and can switch tracks in: an HLS master playlist, one
# media file per rendition, and a WebVTT file per text subtitle.
#
#   bash prep-hls.sh film.mkv [outdir]
#
# It lives in public/ so the library can hand it to whoever is uploading —
# the machine with the files on it is usually not the machine with the repo.
#
# bash (4+) and ffmpeg, nothing else — so it runs the same in Git Bash or WSL
# on Windows as it does on the Mac. macOS still ships bash 3.2, which this needs
# more than; `brew install bash` and run it with that. Run it wherever the file
# is, before uploading.
# Nothing here happens at playback time — the VPS has two cores and no business
# transcoding anything.
#
# Video is copied, not re-encoded, whenever it is already H.264. Audio is
# re-encoded only when it is something a browser cannot decode (AC3, DTS,
# TrueHD, FLAC). Image subtitles (PGS, VOBSUB) cannot be shown by a browser at
# all and are reported rather than silently dropped.
#
# Knobs, all environment variables (the PowerShell twin takes them as switches):
#   PREP_COPY_VIDEO=1      keep the video stream, do not re-encode
#   PREP_REENCODE=1        force H.264 re-encode even when source is H.264
#   PREP_LADDER=1          make a ladder (heights above the source dropped), so
#                          the player can switch resolution; unset = a single
#                          rendition at the source height
#   PREP_LADDER_HEIGHTS    which rungs, comma-separated, tallest first; default
#                          '2160,1440,1080'. 'raw' is a rung that copies the
#                          source stream untouched (its codec and HDR kept), so
#                          '1080,raw' ships a re-encoded 1080 beside the original
#   PREP_LADDER_BITRATES   per-rung overrides, e.g. '2160=16M,1080=8M'
#   PREP_HEIGHT            scale a single encoded rendition to this height
#   PREP_GPU_TONEMAP=1     tonemap HDR on the GPU (Vulkan/libplacebo) if built in
#   PREP_AUTO_HDR=1        raw 4K HDR + tonemapped 1080p/720p SDR ladder
#   PREP_PRESERVE_HDR=1    re-encode a single 10-bit HEVC HDR rendition
#   PREP_COPY_AUDIO=1      also carry the original audio untouched (Apple only)
#   PREP_AUDIO_CHANNELS    '2' (default), '2,6' for stereo+5.1, add 'raw' to copy
#   PREP_VIDEO_BITRATE     re-encode target, default 8M
#   PREP_AUDIO_BITRATE     re-encode target, default 192k (per-layout otherwise)
#   PREP_SEGMENT_SECONDS   default 6
#   PREP_POSTER_SECONDS    default 5
#
# The output is flat and prefixed, because the library treats a Drive
# sub-folder as a series. `<slug>.m3u8` is the video; everything else is named
# `<slug>.part*` or `<slug>.sub-*`, which is how sync tells a playlist to list
# from the pieces it points at.
# macOS's stock bash is 3.2 (frozen at the last GPL2 release) and mishandles
# empty/unset arrays under `set -u`, which this script leans on throughout.
# Everywhere else — Git Bash, WSL, Linux, a brewed bash — is 4+. Ask for it
# rather than sprinkle 3.2 workarounds no other platform needs. Checked before
# `set -o pipefail`, which a non-bash shell would choke on first.
if [ -z "${BASH_VERSINFO:-}" ] || [ "${BASH_VERSINFO[0]}" -lt 4 ]; then
  echo "prep-hls.sh needs bash 4 or newer; this is ${BASH_VERSION:-not bash}." >&2
  echo "On a Mac:  brew install bash   then run:  \$(brew --prefix)/bin/bash prep-hls.sh ..." >&2
  exit 1
fi

set -euo pipefail

input=${1:?usage: prep-hls.sh input.mkv [outdir]}

# One folder per film, named after the file, so several of them can be prepared
# side by side without their parts landing in the same place. A trailing dot or
# space is trimmed because Windows rejects it in a directory name.
dir_name=$(basename "${input%.*}" | sed 's/[ .]*$//')
[ -n "$dir_name" ] || dir_name=hls
outdir=${2:-$(dirname "$input")/$dir_name}

# Everything after the last dot, lowercased and stripped of characters that
# would need escaping in a playlist URI.
slug=$(basename "${input%.*}" | tr ' ' '-' | tr -cd '[:alnum:]._-')
[ -n "$slug" ] || slug=video

mkdir -p "$outdir"
audio_tmp=$(mktemp -d)
trap 'rm -rf "$audio_tmp"' EXIT

# Rendition records for the schemaVersion 2 info.json, written at the very end by
# write-info-json.py. Kept as TSV so a title with quotes or non-ASCII never needs
# JSON escaping here in bash — the python twin serialises it. Recorded at each
# rung/track decision because that is the only place the raw-vs-encoded choice and
# per-rung bitrate are known.
info_vid_rec="$audio_tmp/rec-video.tsv"; : > "$info_vid_rec"
info_aud_rec="$audio_tmp/rec-audio.tsv"; : > "$info_aud_rec"
record_video() { printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" >> "$info_vid_rec"; }
record_audio() { printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9" "${10}" "${11}" >> "$info_aud_rec"; }

# ffprobe's key=value output, parsed in bash: a JSON parser would mean python,
# and python is one more thing to have installed on a Windows box.
list_streams() {
  ffprobe -v error -select_streams "$1" \
    -show_entries stream=index,codec_name,channels,bit_rate:stream_tags=language,title \
    -of default=nw=1 "$input" |
    awk -F= '
      /^index=/ { if (n++) print line; line = "" ; codec = ""; channels = "2"; bitrate = ""; lang = "und"; title = "" }
      /^codec_name=/ { codec = $2 }
      /^channels=/ { channels = $2 }
      /^bit_rate=/ { bitrate = $2 }
      /^TAG:language=/ { lang = $2 }
      /^TAG:title=/ { title = substr($0, index($0, "=") + 1) }
      { line = codec "\t" channels "\t" lang "\t" title "\t" bitrate }
      END { if (n) print line }
    '
}

# The video's codec, profile, level, pixel format and transfer — enough to
# decide whether it needs tonemapping and what to write in the master's CODECS.
# Parsed key=value, because a profile like "Main 10" carries a space that a
# positional read would split.
probe_video() { ffprobe -v error -select_streams v:0 -show_entries "stream=$1" -of default=nw=1:nk=1 "$input" | head -1; }
video_codec=$(probe_video codec_name)
video_profile=$(probe_video profile)
video_level=$(probe_video level)
video_pixfmt=$(probe_video pix_fmt)
video_transfer=$(probe_video color_transfer)
video_primaries=$(probe_video color_primaries)
video_space=$(probe_video color_space)
video_height=$(probe_video height)
video_bitrate=$(probe_video bit_rate)
if ! [[ "${video_bitrate:-}" =~ ^[0-9]+$ ]]; then
  format_bitrate=$(ffprobe -v error -show_entries format=bit_rate -of default=nw=1:nk=1 "$input" | head -1)
  if [[ "${format_bitrate:-}" =~ ^[0-9]+$ ]]; then video_bitrate=$format_bitrate; else video_bitrate=''; fi
fi

# One line per stream: codec, channels, language, title, bit_rate.
audio=$(list_streams a)
subs=$(list_streams s)

if [ -z "$audio" ]; then
  echo "No audio streams in $input" >&2
  exit 1
fi

echo "video: ${video_codec:-none}"

# H.264 plays everywhere. HEVC plays in Safari and nowhere else, and an older
# Chromecast refuses it outright, so it is re-encoded unless PREP_COPY_VIDEO=1
# says the library only ever gets watched somewhere that can take it.
copy_video=0
if { [ "$video_codec" = "h264" ] || [ "${PREP_COPY_VIDEO:-0}" = "1" ]; } && [ "${PREP_REENCODE:-0}" != "1" ] && [ "${PREP_PRESERVE_HDR:-0}" != "1" ]; then
  copy_video=1
fi
out_codec=h264
[ "$copy_video" = "1" ] && out_codec=$video_codec

# Empty by default so the ffmpeg line can expand it even when nothing needs a
# hardware decoder (an unset array trips set -u on older bash).
input_args=()
video_args=()
video_maps=()
nvid=0

# A browser cannot decode 10-bit, so a deep-colour source comes down to
# yuv420p. HDR needs more than a bit-depth cut: without tonemapping, BT.2020/PQ
# read as BT.709 arrives grey and washed out.
is_hdr=0
case "$video_transfer" in smpte2084 | arib-std-b67) is_hdr=1 ;; esac

if [ "${PREP_AUTO_HDR:-0}" = "1" ]; then
  [ "$video_codec" = "hevc" ] && [ "$is_hdr" = "1" ] || { echo 'PREP_AUTO_HDR requires an HEVC HDR source' >&2; exit 1; }
  export PREP_LADDER=1 PREP_GPU_TONEMAP=1
  export PREP_LADDER_HEIGHTS=${PREP_LADDER_HEIGHTS:-raw,1080,720}
  export PREP_AUDIO_CHANNELS=${PREP_AUDIO_CHANNELS:-2,raw}
  copy_video=0
  echo "  HDR Auto: raw HDR + SDR ladder ($PREP_LADDER_HEIGHTS)"
fi
if [ "${PREP_PRESERVE_HDR:-0}" = "1" ]; then
  [ "$is_hdr" = "1" ] || { echo 'PREP_PRESERVE_HDR requires an HDR source' >&2; exit 1; }
  [ -z "${PREP_LADDER:-}" ] || { echo 'PREP_PRESERVE_HDR cannot be combined with a ladder' >&2; exit 1; }
  copy_video=0
  out_codec=hevc
fi

# A 'HYBRID' HDR source carries a Dolby Vision layer inside the HEVC bitstream. A
# copied rung keeps the layer, but fmp4 only writes the dvcC/dvvC signalling box
# under -strict unofficial; without it Apple sees the HDR10 base and misses the
# DV. A re-encoded (tonemapped) rung drops DV either way, so it only matters when
# the source stream is copied.
dovi_probe=$({ ffprobe -v error -select_streams v:0 \
  -show_entries stream_side_data=side_data_type,dv_profile,dv_level -of default=nw=1 "$input" 2>/dev/null || true; } |
  awk -F= '
    /^side_data_type=/ { active = (tolower($2) ~ /dovi|dolby vision/); if (active) found=1; profile=""; level="" }
    active && /^dv_profile=/ { profile=$2 }
    active && /^dv_level=/ { level=$2 }
    END { if (found) print profile "\t" level }
  ')
has_dovi=0; dv_profile=''; dv_level=''
if [ -n "$dovi_probe" ]; then
  has_dovi=1
  dv_profile=${dovi_probe%%$'\t'*}
  dv_level=${dovi_probe##*$'\t'}
fi

is_deep=0
case "$video_pixfmt" in *10le | *10be | *12le | *12be | *16le | *16be | p010* | p012* | p016*) is_deep=1 ;; esac

# The encoder, chosen once: videotoolbox on Apple silicon, NVENC on an NVIDIA
# box, libx264 otherwise. This is the step that takes real time.
ff_encoders=$(ffmpeg -hide_banner -encoders 2>&1)
encoder_works() {
  ffmpeg -hide_banner -loglevel error -f lavfi -i color=size=64x64:rate=1 \
    -frames:v 1 -c:v "$1" -f null - >/dev/null 2>&1
}
if [ "${PREP_PRESERVE_HDR:-0}" = "1" ]; then
  if grep -q hevc_nvenc <<<"$ff_encoders" && encoder_works hevc_nvenc; then venc=hevc_nvenc
  elif grep -q libx265 <<<"$ff_encoders"; then venc=libx265
  else echo 'No HEVC encoder found (need hevc_nvenc or libx265)' >&2; exit 1
  fi
elif grep -q h264_nvenc <<<"$ff_encoders" && encoder_works h264_nvenc; then venc=h264_nvenc
elif grep -q h264_videotoolbox <<<"$ff_encoders" && encoder_works h264_videotoolbox; then venc=h264_videotoolbox
else venc=libx264
fi
if [ "${PREP_REQUIRE_NVENC:-0}" = "1" ] && [ "$copy_video" != "1" ] &&
  [ "$venc" != "h264_nvenc" ] && [ "$venc" != "hevc_nvenc" ]; then
  echo "GPU encoding requested, but NVENC could not encode a test frame. Check Kaggle GPU and ffmpeg/NVIDIA driver compatibility." >&2
  exit 1
fi
if [ "$copy_video" = "1" ] && [ -z "${PREP_LADDER:-}" ]; then
  echo "  video mode: stream copy (no GPU video encoding)"
else
  echo "  video encoder: $venc"
fi

# ffmpeg's filter list, read once — the GPU paths below probe it.
ff_filters=$(ffmpeg -hide_banner -filters 2>&1)

# A ladder re-encodes two or three rungs at once, which otherwise pins the CPU
# on decoding the 4K source and rescaling it once per rung. On NVENC we keep an
# SDR/deep ladder wholly on the GPU: NVDEC decodes, scale_cuda resizes (and
# drops 10-bit to the 8-bit a browser needs) in one pass, NVENC encodes — the
# CPU never touches a frame. A build without scale_cuda falls back to the CPU.
gpu_ladder=0
if [ -n "${PREP_LADDER:-}" ] && [ "$venc" = "h264_nvenc" ] && [ "$is_hdr" = "0" ] &&
  grep -q scale_cuda <<<"$ff_filters"; then
  gpu_ladder=1
fi

# An HDR ladder is the slow one: tonemapping is the cost, and the CPU chain runs
# it on the full frame once per rung. With libplacebo we tonemap once on the GPU
# and split that single SDR result to the rungs (the filter_complex is built in
# the ladder section) — roughly 3x faster. A copy-top-rung ladder keeps the
# per-rung path, so this wants every rung encoded.
gpu_hdr_ladder=0
if [ -n "${PREP_LADDER:-}" ] && [ "$is_hdr" = "1" ] && [ "${PREP_GPU_TONEMAP:-0}" = "1" ] &&
  [ "$venc" = "h264_nvenc" ] && [ "$copy_video" != "1" ] && grep -q libplacebo <<<"$ff_filters"; then
  gpu_hdr_ladder=1
fi

# Rough per-height target bitrates for the ladder, in the range a streaming
# service uses for H.264 — an explicit PREP_VIDEO_BITRATE only overrides the
# single-rendition (no-ladder) encode, not the ladder rungs.
declare -A custom_rung_bitrates=()
if [ -n "${PREP_LADDER_BITRATES:-}" ]; then
  IFS=',' read -r -a bitrate_pairs <<< "$PREP_LADDER_BITRATES"
  for pair in "${bitrate_pairs[@]}"; do
    key=${pair%%=*}; value=${pair#*=}
    if [[ "$key" =~ ^[0-9]+$ ]] && [[ "$value" =~ ^[0-9]+([.][0-9]+)?[MmKk]?$ ]]; then
      custom_rung_bitrates[$key]=$value
    else
      echo "PREP_LADDER_BITRATES expects HEIGHT=RATE pairs, got '$pair'" >&2
      exit 1
    fi
  done
fi
rung_bitrate() {
  local h=$1 requested kbps cap
  if [ -n "${custom_rung_bitrates[$h]:-}" ]; then requested="${custom_rung_bitrates[$h]}"
  elif [ "$h" -ge 2160 ]; then requested=16M
  elif [ "$h" -ge 1440 ]; then requested=10M
  elif [ "$h" -ge 1080 ]; then requested=8M
  elif [ "$h" -ge 720 ]; then requested=4M
  elif [ "$h" -ge 480 ]; then requested=2M
  else requested=1M
  fi
  # Automatic rungs never exceed the source bitrate scaled by height. Explicit
  # per-rung values remain deliberate and are not capped.
  if [ -z "${custom_rung_bitrates[$h]:-}" ] && [[ "${video_bitrate:-}" =~ ^[0-9]+$ ]] && [[ "${video_height:-}" =~ ^[0-9]+$ ]] && [ "$video_height" -gt 0 ]; then
    case "$requested" in *[Mm]) kbps=$(awk -v n="${requested%[Mm]}" 'BEGIN{printf "%d",n*1000}');; *[Kk]) kbps=${requested%[Kk]};; *) kbps=$requested;; esac
    cap=$((video_bitrate / 1000 * h / video_height))
    [ "$cap" -gt 0 ] && [ "$cap" -lt "$kbps" ] && { echo "${cap}k"; return; }
  fi
  echo "$requested"
}

# Adds one encoded video output at index $1, scaled to height $2 (empty keeps
# the source height). Used for every ladder rung and the single re-encode.
add_encoded_video() {
  local i=$1 h=$2 br=$3 vf th
  video_maps+=(-map 0:v:0)
  if [ "$gpu_ladder" = "1" ]; then
    # scale_cuda resizes and lands on 8-bit yuv420p in a single GPU pass; -2
    # keeps the aspect and an even width. A source-height top rung still runs
    # through it — a cheap no-op resize that also does any 10-bit->8-bit drop.
    th=${h:-$video_height}
    video_args+=(-filter:v:"$i" "scale_cuda=-2:${th}:format=yuv420p")
  else
    vf="$base_vf"
    [ -n "$h" ] && vf="${vf:+$vf,}scale=-2:$h"
    [ -n "$vf" ] && video_args+=(-filter:v:"$i" "$vf")
  fi
  video_args+=(-c:v:"$i" "$venc" -b:v:"$i" "$br")
  case "$venc" in h264_videotoolbox) video_args+=(-tag:v:"$i" avc1) ;; esac
  # A ladder runs two or three NVENC sessions at once; p3 keeps them moving,
  # and at a fixed target bitrate the quality cost over the default is slight.
  [ "$venc" = "h264_nvenc" ] && [ -n "${PREP_LADDER:-}" ] && video_args+=(-preset:v:"$i" p3)
  [ "$venc" = "libx264" ] && video_args+=(-preset:v:"$i" medium)
  if [ "${PREP_PRESERVE_HDR:-0}" = "1" ]; then
    video_args+=(-pix_fmt:v:"$i" p010le -tag:v:"$i" hvc1 \
      -color_primaries:v:"$i" "${video_primaries:-bt2020}" -color_trc:v:"$i" "$video_transfer" -colorspace:v:"$i" "${video_space:-bt2020nc}")
  elif [ "$is_hdr" = "1" ]; then
    video_args+=(-color_primaries:v:"$i" bt709 -color_trc:v:"$i" bt709 -colorspace:v:"$i" bt709)
  fi
  # A trailing false test above must not be the function's exit status: set -e
  # would take it down with the caller.
  return 0
}

# The filter chain every encoded rung shares (the scale is appended per rung).
base_vf=""
if [ "${PREP_PRESERVE_HDR:-0}" = "1" ]; then
  base_vf='format=p010le'
  echo "  preserving HDR and re-encoding to 10-bit HEVC (${PREP_VIDEO_BITRATE:-15M})"
elif [ "$is_hdr" = "1" ] && [ "${PREP_GPU_TONEMAP:-0}" = "1" ] && [ "$gpu_hdr_ladder" = "0" ] &&
  [ "$venc" = "h264_nvenc" ] && grep -q libplacebo <<<"$ff_filters"; then
  # Single-stream GPU tonemap (and the lower rungs of a copy-top-rung ladder).
  # NVDEC hands off CUDA frames; libplacebo wants Vulkan, and there is no direct
  # interop, so the one hwdownload in the middle is the price.
  input_args=(-init_hw_device vulkan=vk -filter_hw_device vk -hwaccel cuda -hwaccel_output_format cuda)
  base_vf='hwdownload,format=p010le,libplacebo=tonemapping=bt.2390:colorspace=bt709:color_primaries=bt709:color_trc=bt709:format=yuv420p,hwdownload,format=yuv420p'
  echo "  tonemapping HDR ($video_transfer, $video_pixfmt) on the GPU with libplacebo (bt.2390)"
elif [ "$gpu_hdr_ladder" = "1" ]; then
  # The tonemap+split lives in the filter_complex below; here we just name the
  # Vulkan/CUDA devices it needs.
  input_args=(-init_hw_device vulkan=vk -filter_hw_device vk -hwaccel cuda -hwaccel_output_format cuda)
  echo "  ladder: tonemapping HDR ($video_transfer, $video_pixfmt) once on the GPU, then splitting to the rungs"
elif [ "$is_hdr" = "1" ]; then
  if [ "${PREP_GPU_TONEMAP:-0}" = "1" ] && { [ "$venc" != "h264_nvenc" ] || ! grep -q libplacebo <<<"$ff_filters"; }; then
    echo "  GPU tonemap unavailable on this host — using the CPU" >&2
  fi
  # Linearise PQ, tonemap in float, land back on BT.709.
  base_vf='zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p'
  echo "  tonemapping HDR ($video_transfer, $video_pixfmt) to SDR BT.709 — CPU work, slow"
elif [ "$is_deep" = "1" ] && [ "$gpu_ladder" = "0" ]; then
  base_vf='format=yuv420p'
  echo "  converting $video_pixfmt to 8-bit yuv420p"
fi

if [ "$gpu_ladder" = "1" ]; then
  input_args=(-hwaccel cuda -hwaccel_output_format cuda)
  echo "  ladder: NVDEC decode + scale_cuda on the GPU (the CPU stays free)"
fi

# The ladder request splits into an optional 'raw' rung — the source stream
# copied untouched, its own codec and HDR kept — and numeric heights to encode,
# tallest first, with any taller than the source dropped. A raw rung lets Apple
# devices take the HDR/HEVC original while the encoded rungs cover every other
# browser. With neither left, fall back to a single source-height encoded rung.
want_raw=0
heights=()
IFS=',' read -r -a ladder_tokens <<< "$(echo "${PREP_LADDER_HEIGHTS:-2160,1440,1080}" | tr 'A-Z ' 'a-z')"
for h in "${ladder_tokens[@]}"; do
  [ -n "$h" ] || continue
  if [ "$h" = "raw" ]; then want_raw=1; continue; fi
  case "$h" in ''|*[!0-9]*) echo "PREP_LADDER_HEIGHTS takes heights or 'raw', got '$h'" >&2; exit 1 ;; esac
  if [ -n "$video_height" ] && [ "$video_height" -gt 0 ] && [ "$h" -gt "$video_height" ]; then continue; fi
  case " ${heights[*]:-} " in *" $h "*) continue ;; esac
  heights+=("$h")
done
if [ "${#heights[@]}" -gt 0 ]; then
  # Descending, so index 0 is the top rung.
  IFS=$'\n' heights=($(printf '%s\n' "${heights[@]}" | sort -rn)); unset IFS
elif [ "$want_raw" = "0" ]; then
  heights=("${video_height:-0}")
fi

filter_complex=""
copied_hevc=0

if [ -n "${PREP_LADDER:-}" ]; then
  # The raw rung goes first (it is the tallest, at source height). fmp4 defaults
  # HEVC to the hev1 tag, which Safari refuses; hvc1 keeps the parameter sets
  # where it looks for them.
  if [ "$want_raw" = "1" ]; then
    video_maps+=(-map 0:v:0)
    video_args+=(-c:v:"$nvid" copy)
    [ -n "$video_bitrate" ] && video_args+=(-b:v:"$nvid" "$video_bitrate")
    if [ "$video_codec" = "hevc" ]; then video_args+=(-tag:v:"$nvid" hvc1); copied_hevc=1; fi
    raw_note="${video_height}p $video_codec"
    [ "$is_hdr" = "1" ] && raw_note="$raw_note HDR"
    echo "  rung raw: copying the source stream untouched ($raw_note)"
    record_video "$nvid" 1 "$video_codec" "" ""
    nvid=$((nvid + 1))
  fi

  if [ "$gpu_hdr_ladder" = "1" ] && [ "${#heights[@]}" -gt 0 ]; then
    # Tonemap once, then split that one SDR frame to every encoded rung —
    # rather than tonemapping per rung. Tonemapping is the cost and it scales
    # with pixel count, so scale down to the tallest rung on the GPU *first*
    # (cheap) and tonemap at that size, not at the source 4K: a 1080-max
    # ladder is ~2x faster this way, and a source-height top rung makes the
    # pre-scale a no-op (scale_cuda passes through), so nothing is lost.
    # Output stream indices carry on from $nvid so a raw rung keeps index 0.
    max_h=${heights[0]}
    labels=""
    for ((k = 0; k < ${#heights[@]}; k++)); do labels+="[s$k]"; done
    tonemap="[0:v]scale_cuda=-2:${max_h}:format=p010le,hwdownload,format=p010le,libplacebo=tonemapping=bt.2390:colorspace=bt709:color_primaries=bt709:color_trc=bt709:format=yuv420p,hwdownload,format=yuv420p"
    scale_parts=()
    for k in "${!heights[@]}"; do
      h=${heights[$k]}
      # The tallest rung already sits at $max_h; smaller rungs come down from there.
      scale_parts+=("[s$k]scale=-2:$h[v$k]")
      video_maps+=(-map "[v$k]")
      br=$(rung_bitrate "$h")
      video_args+=(-c:v:"$nvid" "$venc" -b:v:"$nvid" "$br" -preset:v:"$nvid" p3 \
        -color_primaries:v:"$nvid" bt709 -color_trc:v:"$nvid" bt709 -colorspace:v:"$nvid" bt709)
      echo "  rung ${h}p: encoding to H.264 with $venc ($br), GPU-tonemapped"
      record_video "$nvid" 0 h264 "$h" "$br"
      nvid=$((nvid + 1))
    done
    scale_join=""
    for sp in "${scale_parts[@]}"; do scale_join+="$sp;"; done
    scale_join=${scale_join%;}
    filter_complex="$tonemap,split=${#heights[@]}$labels;$scale_join"
  elif [ "${#heights[@]}" -gt 0 ]; then
    # The encoded rungs. Without a raw rung the top one still copies when the
    # source is already deliverable (H.264, or PREP_COPY_VIDEO=1 on an HEVC
    # the viewers can take); a raw rung is the copy instead, so the rest all
    # re-encode.
    for h in "${heights[@]}"; do
      # The rung at source height needs no scale filter.
      hh=$h
      if [ -n "$video_height" ] && [ "$h" = "$video_height" ]; then hh=''; fi
      if [ "$want_raw" = "0" ] && [ "$nvid" = "0" ] && [ "$copy_video" = "1" ] && { [ -z "$hh" ] || [ "$h" = "$video_height" ]; }; then
        video_maps+=(-map 0:v:0)
        video_args+=(-c:v:"$nvid" copy)
        [ -n "$video_bitrate" ] && video_args+=(-b:v:"$nvid" "$video_bitrate")
        if [ "$video_codec" = "hevc" ]; then video_args+=(-tag:v:"$nvid" hvc1); copied_hevc=1; fi
        echo "  rung ${h}p: copying the source stream"
        record_video "$nvid" 1 "$video_codec" "" ""
      else
        add_encoded_video "$nvid" "$hh" "$(rung_bitrate "$h")"
        echo "  rung ${h}p: encoding to H.264 with $venc ($(rung_bitrate "$h"))"
        record_video "$nvid" 0 h264 "$hh" "$(rung_bitrate "$h")"
      fi
      nvid=$((nvid + 1))
    done
  fi
elif [ "$copy_video" = "1" ]; then
  # fmp4 defaults HEVC to the hev1 tag, which Safari refuses outright; hvc1
  # keeps the parameter sets in the sample description where it looks for them.
  video_maps=(-map 0:v:0)
  video_args=(-c:v:0 copy)
  [ -n "$video_bitrate" ] && video_args+=(-b:v:0 "$video_bitrate")
  if [ "$video_codec" = "hevc" ]; then video_args+=(-tag:v:0 hvc1); copied_hevc=1; fi
  nvid=1
  record_video 0 1 "$video_codec" "" ""
  echo "  copying the video stream"
else
  single_height=${PREP_HEIGHT:-}
  if [ -n "$single_height" ] && { ! [[ "$single_height" =~ ^[0-9]+$ ]] || [ "$single_height" -ge "${video_height:-0}" ]; }; then single_height=''; fi
  if [ "${PREP_PRESERVE_HDR:-0}" = "1" ]; then default_vb=15M; else default_vb=8M; fi
  add_encoded_video 0 "$single_height" "${PREP_VIDEO_BITRATE:-$default_vb}"
  nvid=1
  record_video 0 0 "$out_codec" "$single_height" "${PREP_VIDEO_BITRATE:-$default_vb}"
  echo "  re-encoding to $out_codec with $venc (${PREP_VIDEO_BITRATE:-$default_vb})"
fi

# A raw-only ladder copies and never decodes, so any GPU decode/tonemap device
# picked earlier would just sit unused (and the CUDA init can complain). Drop it.
if [ -n "${PREP_LADDER:-}" ] && [ "$want_raw" = "1" ] && [ "${#heights[@]}" -eq 0 ]; then
  input_args=()
fi

# Pull the audio streams into arrays: each layout below walks them again.
a_codec=(); a_channels=(); a_lang=(); a_title=(); a_bitrate=()
while IFS=$'\t' read -r codec channels language title bitrate; do
  [ -n "$codec" ] || continue
  a_codec+=("$codec"); a_channels+=("$channels"); a_lang+=("$language")
  a_title+=("${title:-$language}"); a_bitrate+=("$bitrate")
done <<< "$audio"

first_packet_pts() {
  ffprobe -v error -select_streams "$1" -read_intervals '%+#1' -show_packets \
    -show_entries packet=pts_time -of default=nw=1:nk=1 "$input" | head -1
}
video_start=$(first_packet_pts v:0); video_start=${video_start:-0}
audio_leads=(); audio_align=()
for i in "${!a_codec[@]}"; do
  audio_start=$(first_packet_pts "a:$i"); audio_start=${audio_start:-0}
  lead=$(awk -v a="$audio_start" -v v="$video_start" 'BEGIN { d=a-v; if(d<0)d=0; printf "%.3f",d }')
  audio_leads+=("$lead")
  if awk -v d="$lead" 'BEGIN { exit !(d>0.25) }'; then
    audio_align+=(1); echo "  audio $i starts ${lead}s after video; output will be padded to PTS 0"
  else audio_align+=(0); fi
done

new_padded_raw_audio() {
  local index=$1 lead=$2 codec=$3 channels=$4 layout=stereo bitrate=640k
  case "$channels" in 1) layout=mono;; 2) layout=stereo;; 6) layout=5.1;; 8) layout=7.1;; esac
  [ -n "${a_bitrate[$index]:-}" ] && [ "${a_bitrate[$index]}" != N/A ] && bitrate="$((a_bitrate[index] / 1000))k"
  local sil="$audio_tmp/sil-$index.$codec" orig="$audio_tmp/orig-$index.$codec" list="$audio_tmp/list-$index.txt" padded="$audio_tmp/padded-$index.$codec"
  ffmpeg -nostdin -v error -y -f lavfi -i "anullsrc=channel_layout=$layout:sample_rate=48000" -t "$lead" -c:a "$codec" -b:a "$bitrate" -strict experimental -f "$codec" "$sil" || return 1
  ffmpeg -nostdin -v error -y -i "$input" -map "0:a:$index" -c:a copy -f "$codec" "$orig" || return 1
  printf "file '%s'\nfile '%s'\n" "$sil" "$orig" > "$list"
  ffmpeg -nostdin -v error -y -f concat -safe 0 -i "$list" -c:a copy -f "$codec" "$padded" || return 1
  printf '%s\n' "$padded"
}

# What a rendition's codec is called in a CODECS attribute.
codec_string() {
  case "$1" in
    aac) echo 'mp4a.40.2' ;;
    flac) echo 'fLaC' ;;
    eac3) echo 'ec-3' ;;
    ac3) echo 'ac-3' ;;
    truehd) echo 'mlpa' ;;
    dts) echo 'dtsc' ;;
    *) echo '' ;;
  esac
}

channel_label() {
  case "$1" in
    raw) echo original ;;
    1) echo mono ;;
    2) echo stereo ;;
    6) echo 5.1 ;;
    8) echo 7.1 ;;
    *) echo "${1}ch" ;;
  esac
}

# 192k is a stereo figure and starves 5.1, but scaling it straight up
# overshoots: surround channels are coded jointly and the LFE costs almost
# nothing. 64k per channel lands on 384k for 5.1, which is what Apple's HLS
# spec asks for. An explicit PREP_AUDIO_BITRATE still wins.
bitrate_set=0; [ -n "${PREP_AUDIO_BITRATE+x}" ] && bitrate_set=1
channel_bitrate() {
  if [ "$bitrate_set" = "1" ] || [ "$1" -le 2 ]; then echo "${PREP_AUDIO_BITRATE:-192k}"; else echo "$((64 * $1))k"; fi
}

# One entry per audio rendition: '2' for stereo only, '2,6' for stereo and 5.1
# side by side, '2,6,raw' to carry the untouched original alongside them.
channels_set=0; [ -n "${PREP_AUDIO_CHANNELS+x}" ] && channels_set=1
IFS=',' read -r -a channel_list <<< "$(echo "${PREP_AUDIO_CHANNELS:-2}" | tr 'A-Z ' 'a-z')"
for spec in "${channel_list[@]}"; do
  [ "$spec" = "raw" ] && continue
  case "$spec" in
    ''|*[!0-9]*) echo "PREP_AUDIO_CHANNELS takes 1..8 or 'raw', got '$spec'" >&2; exit 1 ;;
  esac
  { [ "$spec" -lt 1 ] || [ "$spec" -gt 8 ]; } && { echo "PREP_AUDIO_CHANNELS takes 1..8 or 'raw', got '$spec'" >&2; exit 1; }
done
# PREP_COPY_AUDIO adds the original rather than replacing the list, so asking
# for both stereo and the untouched track does not silently drop one of them.
if [ "${PREP_COPY_AUDIO:-0}" = "1" ] && [[ " ${channel_list[*]} " != *" raw "* ]]; then
  if [ "$channels_set" = "1" ]; then channel_list+=(raw); else channel_list=(raw); fi
fi
[ "${#channel_list[@]}" -gt 4 ] && { echo "PREP_AUDIO_CHANNELS takes at most 4 renditions" >&2; exit 1; }

multi=0; [ "${#channel_list[@]}" -gt 1 ] && multi=1

maps=("${video_maps[@]}")
codec_args=()
extra_input_args=()
map_parts=()
groups=(); gbitrate=(); gcodec=()
out_index=0
needs_unofficial=0
needs_experimental=0

# The group index for a name, appending a fresh group the first time it is
# seen, and setting $gi. Not `echo`+`$(...)`: a command substitution runs in a
# subshell, and the array appends below would vanish with it on return.
group_idx() {
  local g=$1 i
  for i in "${!groups[@]}"; do
    if [ "${groups[$i]}" = "$g" ]; then gi=$i; return; fi
  done
  groups+=("$g"); gbitrate+=(0); gcodec+=('')
  gi=$((${#groups[@]} - 1))
}

for spec in "${channel_list[@]}"; do
  # A rendition group per layout. Apple's spec wants surround kept in its own
  # group rather than mixed in beside stereo, and a player picks per group.
  if [ "$multi" = "0" ]; then group=aud; elif [ "$spec" = "raw" ]; then group=araw; else group="a$spec"; fi
  group_idx "$group"

  for i in "${!a_codec[@]}"; do
    codec=${a_codec[$i]}; channels=${a_channels[$i]}; language=${a_lang[$i]}; title=${a_title[$i]}
    source_selector="0:a:$i"
    # info.json record fields, defaulted then refined per branch below.
    a_out_codec=$codec; a_reencoded=0; a_padded=0; a_actual_channels=$channels; a_kbps=0

    if [ "$spec" = "raw" ]; then
      case "$codec" in flac | dts) needs_unofficial=1 ;; truehd) needs_experimental=1 ;; esac
      if [ "${audio_align[$i]:-0}" = "1" ] && { [ "$codec" = aac ] || [ "$codec" = flac ]; }; then
        codec_args+=(-c:a:"$out_index" "$codec" -ac:a:"$out_index" "$channels" -filter:a:"$out_index" 'aresample=async=1:first_pts=0')
        echo "audio $i: late $codec ${channels}ch $language — re-encoding with leading silence"
        a_reencoded=1
      elif [ "${audio_align[$i]:-0}" = "1" ] && { [ "$codec" = eac3 ] || [ "$codec" = ac3 ]; }; then
        if padded=$(new_padded_raw_audio "$i" "${audio_leads[$i]}" "$codec" "$channels"); then
          input_number=$((1 + ${#extra_input_args[@]} / 2))
          extra_input_args+=(-i "$padded")
          source_selector="$input_number:a:0"
          codec_args+=(-c:a:"$out_index" copy)
          echo "audio $i: $codec ${channels}ch $language — copied with ${audio_leads[$i]}s silent preroll"
          a_padded=1
        else
          codec_args+=(-c:a:"$out_index" copy)
          echo "audio $i: could not build silent preroll; copying with original offset" >&2
        fi
      else
        codec_args+=(-c:a:"$out_index" copy)
      fi
      gcodec[$gi]=$(codec_string "$codec")
      # No encoder to ask, so the source's own rate is what the variant carries.
      kbps=640; [ -n "${a_bitrate[$i]}" ] && [ "${a_bitrate[$i]}" != "N/A" ] && kbps=$((a_bitrate[i] / 1000))
      [ "$kbps" -gt "${gbitrate[$gi]}" ] && gbitrate[$gi]=$kbps
      echo "audio $i: $codec ${channels}ch $language — copying untouched"
      a_actual_channels=$channels; a_kbps=$kbps
    else
      chan=$spec
      bitrate=$(channel_bitrate "$chan")
      kbps=${bitrate//[!0-9]/}
      [ "$kbps" -gt "${gbitrate[$gi]}" ] && gbitrate[$gi]=$kbps
      gcodec[$gi]='mp4a.40.2'
      a_out_codec=aac
      if [ "$codec" = "aac" ] && [ "$channels" -le "$chan" ] && [ "${audio_align[$i]:-0}" != "1" ]; then
        codec_args+=(-c:a:"$out_index" copy)
        echo "audio $i: $codec ${channels}ch $language — copying"
        a_actual_channels=$channels
        # A copied AAC carries its own rate; leave it unknown (null) when the
        # source never declared one, matching the PowerShell twin.
        a_kbps=''; [ -n "${a_bitrate[$i]}" ] && [ "${a_bitrate[$i]}" != "N/A" ] && a_kbps=$((a_bitrate[i] / 1000))
      else
        codec_args+=(-c:a:"$out_index" aac -ac:a:"$out_index" "$chan" -b:a:"$out_index" "$bitrate")
        [ "${audio_align[$i]:-0}" = "1" ] && codec_args+=(-filter:a:"$out_index" 'aresample=async=1:first_pts=0')
        echo "audio $i: $codec ${channels}ch $language — re-encoding to $(channel_label "$spec") AAC ($bitrate)"
        a_reencoded=1; a_actual_channels=$chan; a_kbps=$kbps
      fi
    fi

    maps+=(-map "$source_selector")

    # The name ends up in the file name, which is what the library shows in the
    # audio menu. With more than one group the language alone would collide, so
    # the layout goes in too.
    if [ "$multi" = "1" ]; then
      name="$language-$(channel_label "$spec")"
    else
      name=$(echo "$title" | tr ' ' '-' | tr -cd '[:alnum:]._-')
      [ -n "$name" ] || name=$language
    fi
    default=$([ "$out_index" = 0 ] && echo ,default:yes || echo '')
    map_parts+=("a:$out_index,agroup:$group,language:$language,name:$name$default")
    # A raw rung stays 'raw' only while it is a true bitstream copy; a late track
    # re-encoded to repair its start is no longer the untouched original.
    a_raw=0; [ "$spec" = "raw" ] && [ "$a_reencoded" = "0" ] && a_raw=1
    record_audio "$out_index" "$name" "$group" "$i" "$spec" "$a_raw" "$a_padded" "$a_reencoded" "$a_out_codec" "$a_actual_channels" "$a_kbps"
    out_index=$((out_index + 1))
  done
done

# Every video rendition joins the first audio group; a player picks the rung by
# bandwidth and the audio group by codec support.
video_sm=""
for ((vi = 0; vi < nvid; vi++)); do video_sm+="v:$vi,agroup:${groups[0]} "; done
stream_map="${video_sm}${map_parts[*]}"

# Subtitles ride alongside as plain WebVTT files rather than as HLS renditions:
# a <track> element switches them in every browser, and it keeps the master
# playlist to the one thing HLS is needed for, which is the audio.
sub_index=0
while IFS=$'\t' read -r codec channels language title bitrate; do
  [ -n "$codec" ] || continue
  i=$sub_index
  sub_index=$((sub_index + 1))
  case "$codec" in
    subrip | ass | ssa | mov_text | webvtt | text)
      out="$outdir/${slug}.sub-${language}-${i}.vtt"
      ffmpeg -nostdin -v error -y -i "$input" -map "0:s:$i" -c:s webvtt "$out"
      echo "subtitle $i: $codec $language -> $(basename "$out")"
      ;;
    *)
      echo "subtitle $i: $codec $language — image subtitles, skipped (a browser cannot draw them)" >&2
      ;;
  esac
done <<< "$subs"

echo "writing HLS to $outdir/${slug}.m3u8"

# single_file + fmp4: one media file per rendition, indexed by byte ranges in
# the playlist. Thousands of segment files would be unusable in Drive, and the
# proxy already serves ranges.
# An HDR ladder tonemaps once and splits, so it maps filtergraph labels ([v0]…)
# rather than the input stream; everything else filters per output stream.
fc_args=(); [ -n "$filter_complex" ] && fc_args=(-filter_complex "$filter_complex")
# Keep Dolby Vision on a copied HEVC rung: fmp4 needs -strict unofficial to
# write the dvcC/dvvC box, or the DV layer is silently dropped to its HDR10 base.
dv_args=()
if [ "$copied_hevc" = "1" ] && [ "$has_dovi" = "1" ]; then
  dv_args=(-strict unofficial)
  echo "  raw rung: keeping Dolby Vision (writing dvcC via -strict unofficial)"
fi
if [ "$needs_experimental" = "1" ]; then
  dv_args=(-strict experimental)
  echo "  original TrueHD/Atmos: enabling experimental fMP4 signalling"
elif [ "$needs_unofficial" = "1" ] && [ "${#dv_args[@]}" -eq 0 ]; then
  dv_args=(-strict unofficial)
  echo "  original lossless/surround audio: enabling experimental fMP4 signalling"
fi
# -nostdin, on every ffmpeg call here: ffmpeg otherwise polls the terminal for
# its interactive keys (q to quit), and a process in a background process group
# that reads its controlling terminal is stopped by SIGTTIN. That is a job
# frozen at "T" the moment encoding starts, which resumes on SIGCONT and then
# freezes again at the next poll — with no error to explain any of it. Nothing
# here wants keystrokes, so reading stdin is pure downside.
ffmpeg -nostdin -v warning -stats -y "${input_args[@]}" -i "$input" "${extra_input_args[@]}" \
  "${fc_args[@]}" \
  "${maps[@]}" \
  "${video_args[@]}" \
  "${codec_args[@]}" \
  "${dv_args[@]}" \
  -f hls \
  -hls_time "${PREP_SEGMENT_SECONDS:-6}" \
  -hls_playlist_type vod \
  -hls_segment_type fmp4 \
  -hls_flags single_file+independent_segments \
  -hls_fmp4_init_filename "${slug}.part%v-init.mp4" \
  -hls_segment_filename "$outdir/${slug}.part%v.m4s" \
  -master_pl_name "${slug}.m3u8" \
  -var_stream_map "$stream_map" \
  "$outdir/${slug}.part%v.m3u8"

# ffmpeg writes no CODECS attribute for HEVC, and never writes VIDEO-RANGE at
# all. Apple's HLS authoring spec requires both, and Safari will refuse a
# variant it cannot identify, so they get filled in here. It also points its
# one variant at the first audio group and leaves the others unreachable — each
# extra group needs a variant of its own aimed at the same video playlist.
master="$outdir/${slug}.m3u8"
if [ -f "$master" ]; then
  # hvc1.<profile_space><profile_idc>.<compat>.<tier><level>.<constraints>
  hvc=''; all_hvc=0
  if [ "$out_codec" = "hevc" ] || [ "$copied_hevc" = "1" ]; then
    prof=1; case "$video_profile" in *10*) prof=2 ;; esac
    hvc="hvc1.$prof.4.L$video_level.B0"
  fi
  [ "$out_codec" = "hevc" ] && all_hvc=1
  case "$video_transfer" in
    smpte2084) range=PQ ;;
    arib-std-b67) range=HLG ;;
    *) range=SDR ;;
  esac
  first_audio=${gcodec[0]}
  base_kbps=${gbitrate[0]}

  # Cross-compatible Dolby Vision (profile 8.x) rides on the HDR10/HLG base that
  # CODECS already names; a SUPPLEMENTAL-CODECS tag is what makes an Apple device
  # pick up the DV layer rather than just the base. dvh1.<profile>.<level>, both
  # zero-padded. Non-DV players ignore the attribute and take the base.
  dv_supp=''
  if [ "$copied_hevc" = "1" ] && [ "$has_dovi" = "1" ] && [ -n "$dv_profile" ] && [ -n "$dv_level" ]; then
    dv_supp=$(printf ',SUPPLEMENTAL-CODECS="dvh1.%02d.%02d"' "$dv_profile" "$dv_level")
  fi

  # Extra groups, encoded as "group|codec|deltaBps" for the awk pass below.
  extra_spec=''
  if [ "${#groups[@]}" -gt 1 ]; then
    for gi in "${!groups[@]}"; do
      [ "$gi" = "0" ] && continue
      delta=$(( (gbitrate[gi] - base_kbps) * 1000 ))
      extra_spec+="${groups[$gi]}|${gcodec[$gi]}|$delta;"
    done
  fi

  # One awk pass: add CODECS/VIDEO-RANGE to the HEVC variants, then clone every
  # EXT-X-STREAM-INF line once per extra audio group. Splitting on commas would
  # break the quoted CODECS, so the edits are done with match/substr.
  awk -v hvc="$hvc" -v allhvc="$all_hvc" -v range="$range" -v firstaudio="$first_audio" -v extra="$extra_spec" -v dvsupp="$dv_supp" '
    function set_attr(line, key, val,   pre, rest, p) {
      # Replace key="..." if present, else append it.
      p = index(line, key "=\"")
      if (p > 0) {
        pre = substr(line, 1, p - 1)
        rest = substr(line, p + length(key) + 2)
        rest = substr(rest, index(rest, "\"") + 1)
        return pre key "=\"" val "\"" rest
      }
      return line ","key"=\"" val "\""
    }
    function video_of(line,   p, s) {
      # First token inside CODECS="...".
      p = index(line, "CODECS=\"")
      if (p == 0) return ""
      s = substr(line, p + 8)
      s = substr(s, 1, index(s, "\"") - 1)
      if (index(s, ",")) s = substr(s, 1, index(s, ",") - 1)
      return s
    }
    function bump_bandwidth(line, d,   out, s, m, n) {
      out = ""; s = line
      while (match(s, /BANDWIDTH=[0-9]+/)) {
        m = substr(s, RSTART, RLENGTH)
        n = substr(m, index(m, "=") + 1) + d
        out = out substr(s, 1, RSTART - 1) "BANDWIDTH=" n
        s = substr(s, RSTART + RLENGTH)
      }
      return out s
    }
    { lines[NR] = $0 }
    /^#EXT-X-STREAM-INF/ {
      vc = video_of($0)
      is_hvc = allhvc || vc ~ /^(hvc1|hev1|dvh1|dvhe)/
      if (hvc != "" && is_hvc) {
        codecs = firstaudio != "" ? hvc "," firstaudio : hvc
        if (index($0, "CODECS=") == 0) lines[NR] = set_attr($0, "CODECS", codecs)
        # VIDEO-RANGE is an enumerated token, not a quoted string.
        if (index(lines[NR], "VIDEO-RANGE=") == 0)
          lines[NR] = lines[NR] ",VIDEO-RANGE=" range dvsupp
      }
      sinf[++ns] = NR
    }
    END {
      for (i = 1; i <= NR; i++) print lines[i]
      if (extra == "" || ns == 0) exit
      # One clone of every video variant per extra audio group: a ladder has
      # several variants, and each has to be reachable from each group.
      m = split(extra, rows, ";")
      for (r = 1; r <= m; r++) {
        if (rows[r] == "") continue
        split(rows[r], f, "|")
        for (k = 1; k <= ns; k++) {
          base = lines[sinf[k]]
          clone = set_attr(base, "AUDIO", "group_" f[1])
          vpart = video_of(base)
          if (vpart != "" && f[2] != "") clone = set_attr(clone, "CODECS", vpart "," f[2])
          clone = bump_bandwidth(clone, f[3] + 0)
          print clone
          print lines[sinf[k] + 1]
        }
      }
    }
  ' "$master" > "$master.tmp" && mv "$master.tmp" "$master"

  if [ "$out_codec" = "hevc" ]; then
    dv_note=''; [ -n "$dv_supp" ] && dv_note=' + Dolby Vision'
    echo "  master playlist: added CODECS and VIDEO-RANGE=$range$dv_note for Apple"
  fi
  [ "${#groups[@]}" -gt 1 ] && echo "  master playlist: added $(( ${#groups[@]} - 1 )) more variant(s) so every audio group is reachable"
fi

# A .m3u8 has no thumbnail of its own — Drive generates one for a video file,
# not for a text playlist — so the library gets a still to put on the card.
ffmpeg -nostdin -v error -y -ss "${PREP_POSTER_SECONDS:-5}" -i "$input" -frames:v 1 -vf scale=640:-2 \
  "$outdir/${slug}.poster.jpg" 2>/dev/null ||
  ffmpeg -nostdin -v error -y -i "$input" -frames:v 1 -vf scale=640:-2 "$outdir/${slug}.poster.jpg"

# The schemaVersion 2 manifest the library reads: the quality badges plus the
# full per-rendition/per-file detail. Built by the python twin (it serialises the
# JSON and re-probes the source exactly as prep-hls.ps1 does, using the rendition
# records captured above). If python3 is not on this box the bundle still gets the
# tiny legacy sidecar below, so nothing here hard-depends on python — the contract
# at the top of this file holds. HDR is retained by Copy, Preserve HDR and the raw
# rung of HDR Auto; ordinary re-encodes are SDR.
copied_source_video=0
[ "$copy_video" = "1" ] && copied_source_video=1
[ -n "${PREP_LADDER:-}" ] && [ "${want_raw:-0}" = "1" ] && copied_source_video=1
video_target=''
if [ -z "${PREP_LADDER:-}" ] && [ "$copy_video" != "1" ]; then
  video_target="${PREP_VIDEO_BITRATE:-${default_vb:-8M}}"
fi

info_helper="$(dirname "${BASH_SOURCE[0]}")/write-info-json.py"
info_written=0
if [ -f "$info_helper" ] && command -v python3 >/dev/null 2>&1; then
  if INFO_VID_REC="$info_vid_rec" INFO_AUD_REC="$info_aud_rec" \
     INFO_COPY_VIDEO="$copy_video" INFO_PRESERVE_HDR="${PREP_PRESERVE_HDR:-0}" \
     INFO_AUTO_HDR="${PREP_AUTO_HDR:-0}" INFO_HAS_DOVI="$has_dovi" \
     INFO_COPIED_SOURCE_VIDEO="$copied_source_video" INFO_OUT_CODEC="$out_codec" \
     INFO_VIDEO_TARGET_BITRATE="$video_target" \
     python3 "$info_helper" "$input" "$outdir" "$slug"; then
    info_written=1
  else
    echo "  info.json: rich manifest failed; writing the legacy sidecar instead" >&2
  fi
fi

if [ "$info_written" = "0" ]; then
  hdr=null
  if [ "$copy_video" = "1" ] || [ "${PREP_PRESERVE_HDR:-0}" = "1" ] || [ "${PREP_AUTO_HDR:-0}" = "1" ]; then
    case "$video_transfer" in
      smpte2084) hdr='"HDR"' ;;
      arib-std-b67) hdr='"HLG"' ;;
    esac
  fi
  maxch=0
  for spec in "${channel_list[@]}"; do
    if [ "$spec" = "raw" ]; then
      for c in "${a_channels[@]}"; do
        case "$c" in '' | *[!0-9]*) c=0 ;; esac
        if [ "$c" -gt "$maxch" ]; then maxch=$c; fi
      done
    elif [ "$spec" -gt "$maxch" ]; then
      maxch=$spec
    fi
  done
  audio_badge=null
  if [ "$maxch" -ge 8 ]; then audio_badge='"7.1"'; elif [ "$maxch" -ge 6 ]; then audio_badge='"5.1"'; fi
  printf '{"hdr":%s,"audio":%s}\n' "$hdr" "$audio_badge" > "$outdir/${slug}.info.json"
fi

echo
echo "done. Open Upload in the library and select every file in $outdir —"
echo "they are one video, and the queue takes them together:"
ls -1 "$outdir" | sed 's/^/  /'
