#!/usr/bin/env node
// Upload every file in a local folder (an HLS bundle from prep-hls.sh) straight
// into a Drive folder, using the same OAuth client and refresh token the app
// itself uses — so the app owns the files and the next /admin sync picks them up.
//
//   node drive_push.mjs <bundle-dir> [driveFolderId]
//
// Reuses the app's googleapis + pg deps and lib/hls.js's mime map. Reads config
// from .env.local (repo root) the same way Next does. Folder defaults to
// DRIVE_FOLDER_ID.
//
// ponytail: simple per-file upload, no resume across a crash. prep-hls parts are
// tens-to-hundreds of MB and the VPS link is short; add resumable if a push to
// Drive starts failing on the big renditions.
import { readFileSync, readdirSync, statSync, createReadStream } from 'node:fs'
import { join, dirname, basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import { google } from 'googleapis'
import pg from 'pg'
import { uploadMime } from '../lib/hls.js'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')

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

// Find (or create) a sub-folder by name under a parent, so a bundle lands in its
// own Drive folder. Reuse avoids spawning a duplicate series folder when a job is
// retried. Names are escaped for the Drive query language (backslash, quote).
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

async function main() {
  const dir = process.argv[2]
  if (!dir) {
    console.error('usage: drive_push.mjs <bundle-dir> [driveParentFolderId]')
    process.exit(1)
  }
  loadEnv()
  const parentId = process.argv[3] || process.env.DRIVE_FOLDER_ID
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

  const files = readdirSync(dir).filter((n) => statSync(join(dir, n)).isFile())
  if (!files.length) throw new Error(`No files in ${dir}`)

  // The library treats one Drive sub-folder as one series/video, so each bundle
  // gets its own folder (named after the bundle dir) instead of being dumped flat
  // beside every other video. Reused if it already exists, so a retry tops up the
  // same folder rather than creating a second copy of the series.
  const folderName = basename(dir.replace(/[\\/]+$/, '')) || 'video'
  const folderId = await ensureFolder(drive, parentId, folderName)
  console.log(`folder: ${folderName} (${folderId})`)

  // Only library pieces get uploaded; anything else is skipped. Announcing the
  // count up front lets process_media.py turn "uploaded" lines into a percent.
  const uploadable = files.filter((n) => uploadMime(n))
  const skipped = files.filter((n) => !uploadMime(n))
  for (const name of skipped) console.error(`skip (not a library piece): ${name}`)
  console.log(`to upload: ${uploadable.length}`)

  let count = 0
  for (const name of uploadable) {
    const mimeType = uploadMime(name) // hls.js decides; Drive/browser guess wrong for .m4s
    const { data } = await drive.files.create({
      requestBody: { name, parents: [folderId], mimeType },
      media: { mimeType, body: createReadStream(join(dir, name)) },
      fields: 'id,name',
      supportsAllDrives: true,
    })
    count++
    console.log(`uploaded ${data.name} (${data.id})`)
  }
  console.log(`done: ${count} file(s) -> ${folderId}`)
}

main().catch((e) => {
  console.error(`drive_push failed for ${basename(process.argv[2] || '')}:`, e.message)
  process.exit(1)
})
