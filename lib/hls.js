// Playlist rewriting, in plain JS with JSDoc for the same reason lib/range.js
// is: `node --test` runs under Node 20, which cannot import TypeScript, and
// turning a playlist into links that work is exactly the kind of string work
// that deserves a test.

export const PLAYLIST_MIME = 'application/vnd.apple.mpegurl'

/** The file types an HLS bundle is made of, and what to call them when the
 *  browser hands over a file it has no type for — which is every .m4s. */
const BUNDLE_TYPES = {
  '.m3u8': PLAYLIST_MIME,
  '.m4s': 'video/mp4',
  '.mp4': 'video/mp4',
  '.vtt': 'text/vtt',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  // The quality sidecar prep-hls writes beside the bundle: HDR and audio layout
  // the manifest cannot carry. Uploaded like any other piece; sync reads it.
  '.json': 'application/json',
}

/**
 * What to tell Drive a file is. Browsers report nothing for .m4s and disagree
 * about .m3u8, and a file stored as application/octet-stream is one the player
 * cannot use later.
 * @param {string} name
 * @param {string} [browserType]
 * @returns {string | null} null when this is not something the library takes
 */
export function uploadMime(name, browserType) {
  if (browserType && browserType.startsWith('video/')) return browserType
  const extension = name.slice(name.lastIndexOf('.')).toLowerCase()
  return BUNDLE_TYPES[extension] ?? null
}

/**
 * What counts as a video in the library rather than a piece of one: a plain
 * video file, or an HLS master playlist — which is `film.m3u8`, never
 * `film.partThai.m3u8`. Used by sync when it reads Drive, and by the upload
 * form to know how many videos a pile of files actually is.
 * @param {string} name
 * @param {string | null | undefined} [mimeType]
 * @returns {boolean}
 */
export function isLibraryEntry(name, mimeType) {
  if (name.endsWith('.m3u8')) return !name.slice(0, -'.m3u8'.length).includes('.part')
  return Boolean(mimeType && mimeType.startsWith('video/'))
}

/**
 * Is this file one of the pieces of the bundle led by `masterName`? The pieces
 * sit beside the master and share its name: `film.m3u8` owns `film.part0.m4s`,
 * `film.sub-eng-0.vtt`, `film.poster.jpg`.
 *
 * The second clause is why this is a function and not a `startsWith`. Drive
 * lets two files share a name, which an upload run twice will do, and both
 * masters then carry the same prefix — so each would claim the other as one of
 * its own pieces and neither would be listed as a video at all.
 *
 * That clause has to test for a master *playlist*, not merely for a library
 * entry: `isLibraryEntry` is true of anything Drive calls video/*, and Drive
 * calls an .m4s rendition exactly that. Widen it and every rendition escapes
 * the bundle to become a video of its own, several per film.
 *
 * @param {string} masterName name of the master playlist, ending in .m3u8
 * @param {string} name the neighbouring file being judged
 * @param {string | null | undefined} [mimeType] what Drive calls it
 * @returns {boolean}
 */
export function isBundlePart(masterName, name, mimeType) {
  const prefix = `${masterName.slice(0, -'.m3u8'.length)}.`
  if (!name.startsWith(prefix)) return false
  return !(name.endsWith('.m3u8') && isLibraryEntry(name, mimeType))
}

/**
 * Drive labels a .m4s or a .vtt however it feels; the browser needs the truth.
 * @param {string} name
 * @returns {string}
 */
export function partMime(name) {
  if (name.endsWith('.m3u8')) return PLAYLIST_MIME
  if (name.endsWith('.vtt')) return 'text/vtt; charset=utf-8'
  if (name.endsWith('.jpg') || name.endsWith('.jpeg')) return 'image/jpeg'
  // An fMP4 segment and its init header are both plain MP4 to a demuxer.
  return 'video/mp4'
}

/**
 * `film.partThai.m3u8` beside `film.m3u8` becomes "Thai". ffmpeg writes
 * NAME="audio_1", which tells a viewer nothing about which track it is.
 * @param {string} partName
 * @param {string} videoName
 * @returns {string}
 */
export function trackLabel(partName, videoName) {
  const prefix = `${videoName.replace(/\.m3u8$/, '')}.`
  const rest = partName.startsWith(prefix) ? partName.slice(prefix.length) : partName
  return rest.replace(/^part/, '').replace(/\.[^.]+$/, '') || rest
}

// Audio codecs a browser's Media Source Extensions cannot decode, so hls.js
// throws CHUNK_DEMUXER_ERROR_APPEND_FAILED and the whole pipeline dies the
// moment such a track is selected. Dolby (AC-3/E-AC-3) and DTS are passthrough
// formats a prep keeps untouched from a disc; only AAC is universal in-browser.
const UNPLAYABLE_AUDIO = /\b(ec-3|ac-3|dts[a-z-]*|truehd|mlpa)\b/i

/**
 * Collapse a master playlist's audio renditions into a single group, dropping
 * any the browser cannot actually decode.
 *
 * prep-hls.ps1 puts each channel layout — stereo, 5.1, the untouched original —
 * in its own AUDIO group and clones the video variant once per group so ffmpeg
 * emits them all. hls.js then exposes only the audio group of the variant its
 * ABR settles on (always the first), so 5.1 and the original were unreachable
 * from the track menu. Merging every audio rendition into one group under a
 * single variant makes the rest selectable.
 *
 * But the "original" group is often a passthrough EC-3 (Dolby Digital Plus)
 * track, and Chrome's MSE cannot decode it — selecting it breaks playback with
 * an append error. An audio group inherits its codec from the STREAM-INF that
 * points at it, so any group whose variant declares a Dolby/DTS codec is
 * dropped rather than offered. The AAC stereo and 5.1 renditions remain.
 *
 * A single-group master with nothing to prune — every bundle prep-hls.sh
 * writes — is returned untouched, as is any child (media) playlist.
 * @param {string} master
 * @returns {string}
 */
export function mergeAudioGroups(master) {
  const lines = master.split('\n')

  // An audio group's codec is the audio half of the CODECS on the STREAM-INF
  // that references it.
  const groupCodec = new Map()
  for (const line of lines) {
    const trimmed = line.trim()
    if (!trimmed.startsWith('#EXT-X-STREAM-INF:')) continue
    const group = trimmed.match(/AUDIO="([^"]+)"/)
    const codecs = trimmed.match(/CODECS="([^"]+)"/)
    if (group) groupCodec.set(group[1], codecs ? codecs[1] : '')
  }
  const playable = (group) => !UNPLAYABLE_AUDIO.test(groupCodec.get(group) ?? '')

  const keep = new Set()
  let anyDropped = false
  for (const line of lines) {
    const trimmed = line.trim()
    if (trimmed.startsWith('#EXT-X-MEDIA:') && /TYPE=AUDIO/.test(trimmed)) {
      const match = trimmed.match(/GROUP-ID="([^"]+)"/)
      if (!match) continue
      if (playable(match[1])) keep.add(match[1])
      else anyDropped = true
    }
  }
  // Nothing decodable to work with — leave the playlist exactly as it came,
  // rather than stripping it down to a variant with no audio.
  if (keep.size === 0) return master
  // Already one playable group and nothing to prune: no rewrite needed.
  if (keep.size < 2 && !anyDropped) return master

  const [merged] = keep
  const out = []
  const seenUri = new Set()
  let defaulted = false

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const trimmed = line.trim()

    if (trimmed.startsWith('#EXT-X-MEDIA:') && /TYPE=AUDIO/.test(trimmed)) {
      const match = trimmed.match(/GROUP-ID="([^"]+)"/)
      if (match && !playable(match[1])) continue // drop what the browser can't play
      let rewritten = line.replace(/GROUP-ID="[^"]+"/, `GROUP-ID="${merged}"`)
      // One DEFAULT for the group — the first — or a player joining it has none.
      if (/DEFAULT=YES/.test(rewritten)) {
        if (defaulted) rewritten = rewritten.replace(/DEFAULT=YES/, 'DEFAULT=NO')
        else defaulted = true
      }
      out.push(rewritten)
      continue
    }

    if (trimmed.startsWith('#EXT-X-STREAM-INF:')) {
      // Each video rendition was cloned once per audio group so every group was
      // reachable; those clones repeat a URI already emitted and collapse into
      // one variant pointed at the merged group. A ladder's distinct renditions
      // point at different URIs, so keep one variant per URI — dropping them all
      // but the first would throw the ladder away.
      let j = i + 1
      while (j < lines.length && (lines[j].trim() === '' || lines[j].trim().startsWith('#'))) j++
      const uri = j < lines.length ? lines[j].trim() : null
      if (uri && seenUri.has(uri)) {
        i = j // skip the clone's STREAM-INF and its URI line
        continue
      }
      if (uri) seenUri.add(uri)
      out.push(line.replace(/AUDIO="[^"]+"/, `AUDIO="${merged}"`))
      continue
    }

    out.push(line)
  }

  return out.join('\n')
}

/**
 * Rewrites a playlist so every URI in it points back through this app.
 *
 * The files live in Drive under ids the playlist knows nothing about, and its
 * relative URIs would otherwise resolve against /api/hls and hit nothing.
 * Playlists point at /api/hls so their own children get rewritten in turn;
 * everything else points at the range proxy.
 *
 * `sign` is passed only when the request itself arrived signed — a Chromecast,
 * which has no cookies — because then every child it fetches needs its own
 * signature too.
 *
 * @param {string} body
 * @param {string} videoName file name of the master playlist
 * @param {Array<{ partDriveId: string, name: string }>} parts
 * @param {((driveId: string) => Promise<string>) | null} sign
 * @returns {Promise<string>}
 */
export async function rewritePlaylist(body, videoName, parts, sign) {
  body = mergeAudioGroups(body)
  const ids = new Map(parts.map((part) => [part.name, part.partDriveId]))

  const target = async (name) => {
    const id = ids.get(name)
    if (!id) return null
    const route = name.endsWith('.m3u8') ? 'hls' : 'video'
    const token = sign ? `?t=${encodeURIComponent(await sign(id))}` : ''
    return `/api/${route}/${id}${token}`
  }

  const out = []

  for (const line of body.split('\n')) {
    const trimmed = line.trim()

    if (trimmed.startsWith('#')) {
      // URI="film.partThai.m3u8", as used by EXT-X-MEDIA and EXT-X-MAP.
      const match = trimmed.match(/URI="([^"]+)"/)
      let rewritten = line
      if (match) {
        const url = await target(match[1])
        if (url) rewritten = rewritten.replace(match[1], url)
        if (trimmed.startsWith('#EXT-X-MEDIA:')) {
          rewritten = rewritten.replace(/NAME="[^"]*"/, `NAME="${trackLabel(match[1], videoName)}"`)
        }
      }
      if (trimmed.startsWith('#EXT-X-STREAM-INF:')) {
        // Drop the Dolby Vision signalling. A browser that cannot decode dvh1
        // would otherwise have hls.js discard the whole rung — losing a 4K HEVC
        // HDR10 base it can actually play. Without the SUPPLEMENTAL-CODECS the
        // rung is judged on its base CODECS; a DV device falls back to that same
        // HDR10 base layer rather than getting the DV enhancement.
        rewritten = rewritten.replace(/,?\s*SUPPLEMENTAL-CODECS="[^"]*"/i, '')
      }
      out.push(rewritten)
      continue
    }

    if (trimmed === '') {
      out.push(line)
      continue
    }

    out.push((await target(trimmed)) ?? line)
  }

  return out.join('\n')
}

/**
 * How much of a set of file names is the same on every one of them. The files
 * of an HLS bundle all carry their video's name, so listing them whole is one
 * sentence repeated a dozen times — only the tail tells them apart.
 * @param {string[]} names
 * @returns {number} characters to cut from the front of each
 */
export function sharedPrefix(names) {
  if (names.length < 2) return 0
  const [first] = names
  let length = 0
  while (length < first.length && names.every((name) => name[length] === first[length])) length++
  // A name that is entirely prefix would list as nothing at all.
  return names.every((name) => name.length > length) ? length : 0
}

/**
 * The pixel size a master playlist advertises for its video, from the first
 * `RESOLUTION=WxH` on an EXT-X-STREAM-INF. Drive never fills this in for a
 * `.m3u8`, so sync reads it off the playlist instead.
 * @param {string} master master playlist text
 * @returns {{ width: number, height: number } | null}
 */
export function masterResolution(master) {
  const match = master.match(/RESOLUTION=(\d+)x(\d+)/i)
  return match ? { width: Number(match[1]), height: Number(match[2]) } : null
}

/**
 * Every distinct video rendition in a master, with the size it advertises: one
 * entry per media playlist a STREAM-INF points at, deduped because an audio
 * group's extra variants point the same video playlist at a different group.
 * Used by sync to record a resolution per part, not just the master's highest.
 * @param {string} master master playlist text
 * @returns {Array<{ uri: string, width: number | null, height: number | null }>}
 */
export function masterRenditions(master) {
  const lines = master.split('\n').map((line) => line.trim())
  const out = []
  const seen = new Set()
  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].startsWith('#EXT-X-STREAM-INF:')) continue
    const res = lines[i].match(/RESOLUTION=(\d+)x(\d+)/i)
    let uri = null
    for (let j = i + 1; j < lines.length; j++) {
      if (lines[j] && !lines[j].startsWith('#')) {
        uri = lines[j]
        break
      }
    }
    if (!uri || seen.has(uri)) continue
    seen.add(uri)
    out.push({
      uri,
      width: res ? Number(res[1]) : null,
      height: res ? Number(res[2]) : null,
    })
  }
  return out
}

/**
 * The video rendition a master points at — the first URI after a STREAM-INF,
 * which is the media playlist whose EXTINFs add up to the running time.
 * @param {string} master master playlist text
 * @returns {string | null}
 */
export function videoRenditionUri(master) {
  const lines = master.split('\n').map((line) => line.trim())
  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].startsWith('#EXT-X-STREAM-INF:')) continue
    // The URI is the next line that is neither blank nor a tag.
    for (let j = i + 1; j < lines.length; j++) {
      if (lines[j] && !lines[j].startsWith('#')) return lines[j]
    }
  }
  return null
}

/**
 * The running time of a media playlist, in seconds, as the sum of its EXTINF
 * durations. Zero when there are none — not a playlist we can read.
 * @param {string} media media playlist text
 * @returns {number}
 */
export function playlistDuration(media) {
  let total = 0
  for (const match of media.matchAll(/#EXTINF:\s*([\d.]+)/gi)) total += Number(match[1])
  return total
}
