#!/usr/bin/env node
// Upload original (pre-HLS) video files straight to Drive, each into its own
// sub-folder named after the file — the library treats a Drive sub-folder as a
// series, the same layout drive_push.mjs gives an HLS bundle. Kept separate from
// drive_push.mjs (which walks an HLS bundle dir and only takes library pieces, so
// it skips a raw .mkv) so neither has to grow a mode flag.
//
//   node drive_upload.mjs <parentFolderId> <file> [file ...]
//
// Progress is per file: it prints `to upload: N` up front and one `uploaded
// <name>` line each, which process_media.py turns into a percent.
import { readFileSync, createReadStream, statSync } from 'node:fs'
import { join, dirname, basename, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import https from 'node:https'
import { PassThrough } from 'node:stream'
import { google } from 'googleapis'
import pg from 'pg'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')

// Drive/browsers guess wrong for raw containers; be explicit so the library sees
// a video/* file (isLibraryEntry) rather than application/octet-stream.
const MIME = {
  '.mp4': 'video/mp4', '.m4v': 'video/x-m4v', '.mkv': 'video/x-matroska',
  '.mov': 'video/quicktime', '.webm': 'video/webm', '.ts': 'video/mp2t',
  '.m2ts': 'video/mp2t', '.mts': 'video/mp2t', '.avi': 'video/x-msvideo',
  '.wmv': 'video/x-ms-wmv', '.flv': 'video/x-flv',
}
function mimeFor(name) {
  return MIME[extname(name).toLowerCase()] || 'application/octet-stream'
}

// Minimal .env.local loader — Next reads it at boot, a bare `node` does not.
function loadEnv() {
  let text
  try {
    text = readFileSync(join(root, '.env.local'), 'utf8')
  } catch {
    return
  }
  for (const line of text.split('\n')) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/)
    if (!m) continue
    let val = m[2].trim()
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1)
    }
    if (!(m[1] in process.env)) process.env[m[1]] = val
  }
}

async function refreshToken() {
  const url = process.env.DATABASE_URL
  if (url) {
    const client = new pg.Client({ connectionString: url })
    try {
      await client.connect()
      const { rows } = await client.query('select value from settings where key = $1', ['drive_refresh_token'])
      if (rows[0]?.value) return rows[0].value
    } catch (e) {
      console.error('warning: could not read token from DB:', e.message)
    } finally {
      await client.end().catch(() => {})
    }
  }
  return process.env.GOOGLE_REFRESH_TOKEN ?? null
}

// Find (or create) a sub-folder by name under a parent. Reuse avoids a duplicate
// series folder on a retry. Names are escaped for the Drive query language.
async function ensureFolder(drive, parentId, name) {
  const escaped = name.replace(/\\/g, '\\\\').replace(/'/g, "\\'")
  const { data } = await drive.files.list({
    q: `'${parentId}' in parents and name = '${escaped}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false`,
    fields: 'files(id,name)',
    pageSize: 1,
    supportsAllDrives: true,
    includeItemsFromAllDrives: true,
  })
  if (data.files && data.files.length) return data.files[0].id
  const created = await drive.files.create({
    requestBody: { name, parents: [parentId], mimeType: 'application/vnd.google-apps.folder' },
    fields: 'id',
    supportsAllDrives: true,
  })
  return created.data.id
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// Open a resumable upload session; returns the session URI to PUT bytes to.
async function initiateSession(auth, folderId, name, mimeType, total) {
  const { token } = await auth.getAccessToken()
  const meta = JSON.stringify({ name, parents: [folderId], mimeType })
  return new Promise((resolve, reject) => {
    const req = https.request({
      method: 'POST',
      hostname: 'www.googleapis.com',
      path: '/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true&fields=id,name',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json; charset=UTF-8',
        'Content-Length': Buffer.byteLength(meta),
        'X-Upload-Content-Type': mimeType,
        'X-Upload-Content-Length': String(total),
      },
    }, (res) => {
      let body = ''
      res.on('data', (d) => { body += d })
      res.on('end', () => {
        if ((res.statusCode === 200 || res.statusCode === 201) && res.headers.location) {
          resolve(res.headers.location)
        } else {
          reject(new Error(`resumable initiate failed ${res.statusCode}: ${body.slice(0, 200)}`))
        }
      })
    })
    req.on('error', reject)
    req.end(meta)
  })
}

// PUT the bytes from `offset` to the end. Resolves {done, data} on 200/201, or
// {done:false} on a 308 (partial). `onBytes(cumulativeForThisFile)` fires as the
// socket drains, so a caller can report byte-level progress mid-request.
function putRemainder(sessionUri, token, filePath, offset, total, onBytes) {
  const url = new URL(sessionUri)
  return new Promise((resolve, reject) => {
    const req = https.request({
      method: 'PUT',
      hostname: url.hostname,
      path: url.pathname + url.search,
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Length': String(total - offset),
        'Content-Range': `bytes ${offset}-${total - 1}/${total}`,
      },
    }, (res) => {
      let body = ''
      res.on('data', (d) => { body += d })
      res.on('end', () => {
        if (res.statusCode === 200 || res.statusCode === 201) {
          let data = {}
          try { data = JSON.parse(body) } catch { /* keep {} */ }
          resolve({ done: true, data })
        } else if (res.statusCode === 308) {
          resolve({ done: false })
        } else {
          reject(new Error(`resumable PUT failed ${res.statusCode}: ${body.slice(0, 200)}`))
        }
      })
    })
    req.on('error', reject)
    const counter = new PassThrough()
    let sent = 0
    counter.on('data', (c) => { sent += c.length; onBytes(offset + sent) })
    createReadStream(filePath, { start: offset }).on('error', reject).pipe(counter).pipe(req)
  })
}

// Ask the server how many bytes it already has, so a retry resumes rather than
// restarts. Returns the next byte offset to send (== total when already done).
function queryOffset(sessionUri, token, total) {
  const url = new URL(sessionUri)
  return new Promise((resolve, reject) => {
    const req = https.request({
      method: 'PUT',
      hostname: url.hostname,
      path: url.pathname + url.search,
      headers: { Authorization: `Bearer ${token}`, 'Content-Length': '0', 'Content-Range': `bytes */${total}` },
    }, (res) => {
      res.resume()
      res.on('end', () => {
        if (res.statusCode === 308) {
          const range = res.headers.range
          const m = range && /bytes=0-(\d+)/.exec(range)
          resolve(m ? parseInt(m[1], 10) + 1 : 0)
        } else if (res.statusCode === 200 || res.statusCode === 201) {
          resolve(total)
        } else {
          reject(new Error(`resumable query failed ${res.statusCode}`))
        }
      })
    })
    req.on('error', reject)
    req.end()
  })
}

// Upload one file with a resumable session: a single streamed PUT in the happy
// path, and on any network/5xx failure it re-queries the confirmed offset and
// resumes from there (up to a few attempts with exponential backoff).
async function resumableUpload(drive, auth, folderId, filePath, name, mimeType, total, onBytes) {
  if (total === 0) {
    const { data } = await drive.files.create({
      requestBody: { name, parents: [folderId], mimeType },
      media: { mimeType, body: createReadStream(filePath) },
      fields: 'id,name',
      supportsAllDrives: true,
    })
    return data
  }
  const sessionUri = await initiateSession(auth, folderId, name, mimeType, total)
  let offset = 0
  const maxAttempts = 6
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      const { token } = await auth.getAccessToken()
      const r = await putRemainder(sessionUri, token, filePath, offset, total, onBytes)
      if (r.done) return r.data
      offset = await queryOffset(sessionUri, token, total)
    } catch (err) {
      if (attempt >= maxAttempts) throw err
      console.error(`  retry ${attempt}/${maxAttempts - 1} after: ${err.message}`)
      await sleep(Math.min(30000, 1000 * 2 ** (attempt - 1)))
      try {
        const { token } = await auth.getAccessToken()
        offset = await queryOffset(sessionUri, token, total)
      } catch { /* keep last offset */ }
    }
  }
  throw new Error(`resumable upload exhausted retries for ${name}`)
}

async function main() {
  const parentArg = process.argv[2]
  const files = process.argv.slice(3)
  if (!parentArg || !files.length) {
    console.error('usage: drive_upload.mjs <parentFolderId> <file> [file ...]')
    process.exit(1)
  }
  loadEnv()
  const parentId = parentArg === '-' ? process.env.DRIVE_FOLDER_ID : parentArg
  if (!parentId) throw new Error('No target folder — pass one or set DRIVE_FOLDER_ID')

  const token = await refreshToken()
  if (!token) throw new Error('Drive not connected — no drive_refresh_token in DB and no GOOGLE_REFRESH_TOKEN')

  const auth = new google.auth.OAuth2(
    process.env.GOOGLE_CLIENT_ID,
    process.env.GOOGLE_CLIENT_SECRET,
    process.env.GOOGLE_REDIRECT_URI,
  )
  auth.setCredentials({ refresh_token: token })
  const drive = google.drive({ version: 'v3', auth })

  const sizes = files.map((f) => {
    const s = statSync(f)
    if (!s.isFile()) throw new Error(`not a file: ${f}`)
    return s.size
  })
  const grandTotal = sizes.reduce((a, b) => a + b, 0) || 1
  console.log(`uploading ${files.length} file(s) to Drive…`)

  // Byte-level progress across ALL files, sampled on a fixed 2s tick so the web
  // UI's bar and the speed reading update steadily regardless of chunk timing.
  // process_media.py turns each `UPLOAD_PCT <pct> <bytesPerSec>` line into the
  // `| pct% | <speed>` the UI reads, plus the mirrored state value.
  let globalUploaded = 0
  let tickBytes = 0
  let tickAt = Date.now()
  const tick = () => {
    const now = Date.now()
    const dt = (now - tickAt) / 1000
    const bps = dt > 0 ? Math.max(0, Math.round((globalUploaded - tickBytes) / dt)) : 0
    const pct = Math.min(100, Math.floor(globalUploaded * 100 / grandTotal))
    console.log(`UPLOAD_PCT ${pct} ${bps}`)
    tickBytes = globalUploaded
    tickAt = now
  }
  console.log('UPLOAD_PCT 0 0')
  const timer = setInterval(tick, 2000)

  let count = 0
  try {
    let completedBytes = 0
    for (let i = 0; i < files.length; i++) {
      const f = files[i]
      const name = basename(f)
      const stem = name.replace(/\.[^.]+$/, '') || name
      const folderId = await ensureFolder(drive, parentId, stem)
      const mimeType = mimeFor(name)
      const onBytes = (fileSent) => { globalUploaded = completedBytes + Math.min(fileSent, sizes[i]) }
      const data = await resumableUpload(drive, auth, folderId, f, name, mimeType, sizes[i], onBytes)
      completedBytes += sizes[i]
      globalUploaded = completedBytes
      count++
      console.log(`uploaded ${data.name || name} (${data.id || '?'}) -> ${stem}`)
    }
  } finally {
    clearInterval(timer)
  }
  console.log('UPLOAD_PCT 100 0')
  console.log(`done: ${count} file(s) -> ${parentId}`)
}

main().catch((e) => {
  console.error(`drive_upload failed:`, e.message)
  process.exit(1)
})
