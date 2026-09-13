#!/usr/bin/env node
// List or delete files in the Drive library folder, using the same OAuth client
// the app and drive_push use. Backs the "Files" tab of bili_web.py.
//
//   node drive_files.mjs list [folderId]        -> JSON [{id,name,size}] to stdout
//   node drive_files.mjs delete <id> [<id>...]  -> delete each (app-created only)
//
// The grant is drive.file, so Drive refuses to delete anything this app did not
// create — a hard safety net under the UI's own confirm step.
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { google } from 'googleapis'
import pg from 'pg'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')

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

async function driveClient() {
  const token = await refreshToken()
  if (!token) throw new Error('Drive not connected — no refresh token')
  const auth = new google.auth.OAuth2(
    process.env.GOOGLE_CLIENT_ID,
    process.env.GOOGLE_CLIENT_SECRET,
    process.env.GOOGLE_REDIRECT_URI,
  )
  auth.setCredentials({ refresh_token: token })
  return google.drive({ version: 'v3', auth })
}

async function main() {
  const cmd = process.argv[2]
  loadEnv()
  const drive = await driveClient()

  if (cmd === 'list') {
    const folderId = process.argv[3] || process.env.DRIVE_FOLDER_ID
    if (!folderId) throw new Error('No folder — pass one or set DRIVE_FOLDER_ID')
    const out = []
    let pageToken
    do {
      const { data } = await drive.files.list({
        q: `'${folderId}' in parents and trashed=false`,
        fields: 'nextPageToken, files(id,name,size,mimeType)',
        pageSize: 1000,
        pageToken,
        supportsAllDrives: true,
        includeItemsFromAllDrives: true,
      })
      // hide sub-folders (a series lives in one) so the UI can't nuke a whole
      // series folder in a single click — only loose library files are listed.
      for (const f of data.files || []) {
        if (f.mimeType !== 'application/vnd.google-apps.folder') out.push(f)
      }
      pageToken = data.nextPageToken
    } while (pageToken)
    console.log(JSON.stringify(out))
    return
  }

  if (cmd === 'delete') {
    const ids = process.argv.slice(3)
    if (!ids.length) throw new Error('no ids given')
    const result = { deleted: [], failed: [] }
    for (const id of ids) {
      try {
        await drive.files.delete({ fileId: id, supportsAllDrives: true })
        result.deleted.push(id)
      } catch (e) {
        result.failed.push({ id, error: e.message })
      }
    }
    console.log(JSON.stringify(result))
    return
  }

  throw new Error('usage: drive_files.mjs list [folderId] | delete <id>...')
}

main().catch((e) => {
  console.error(e.message)
  process.exit(1)
})
