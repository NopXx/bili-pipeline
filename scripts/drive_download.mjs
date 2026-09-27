#!/usr/bin/env node
// Download one binary Drive file into the normal downloads area. A .part file
// is retained on failure/cancel so a retry can request only the missing bytes.
import { createWriteStream, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, statSync, writeFileSync } from 'node:fs'
import { basename, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'
import { pipeline } from 'node:stream/promises'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const videoExtensions = /\.(mp4|mkv|mov|webm|m4v|ts|m2ts|avi)$/i

function loadEnv() {
  let content
  try { content = readFileSync(join(root, '.env.local'), 'utf8') } catch { return }
  for (const line of content.split('\n')) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/)
    if (!match || match[1] in process.env) continue
    let value = match[2].trim()
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1)
    process.env[match[1]] = value
  }
}

async function refreshToken() {
  if (process.env.DATABASE_URL) {
    const { default: pg } = await import('pg')
    const client = new pg.Client({ connectionString: process.env.DATABASE_URL })
    try {
      await client.connect()
      const { rows } = await client.query('select value from settings where key = $1', ['drive_refresh_token'])
      if (rows[0]?.value) return rows[0].value
    } catch (error) {
      console.error(`warning: could not read Drive token from DB: ${error.message}`)
    } finally { await client.end().catch(() => {}) }
  }
  return process.env.GOOGLE_REFRESH_TOKEN || null
}

function state(path, changes) {
  let previous = {}
  try { previous = JSON.parse(readFileSync(path, 'utf8')) } catch { /* first update */ }
  const temporary = `${path}.drive-${process.pid}.tmp`
  writeFileSync(temporary, JSON.stringify({ ...previous, ...changes }))
  renameSync(temporary, path)
}

function safeName(name) {
  const cleaned = basename(String(name || 'download').replace(/[\\/\x00-\x1f\x7f]/g, '_')).replace(/[. ]+$/g, '').slice(0, 200)
  return cleaned || 'download'
}

async function downloadWithRclone(remote, fileId, resourceKey, destination, statePath) {
  const remoteName = remote.split(':', 1)[0]
  if (!/^[A-Za-z0-9_-]+$/.test(remoteName)) throw new Error('invalid BILI_RCLONE_REMOTE')
  const folder = join(destination, fileId)
  mkdirSync(folder, { recursive: true })
  const args = ['backend', 'copyid', `${remoteName}:`, fileId, `${folder}/`, '--stats=1s', '--stats-one-line', '--stats-log-level=NOTICE']
  if (resourceKey) args.push(`--drive-resource-key=${resourceKey}`)
  state(statePath, { kind: 'drive_download', phase: 'drive', status: 'downloading', progress: 0 })
  console.log(`Drive download via rclone: ${fileId}`)
  const child = spawn('rclone', args, { stdio: ['ignore', 'pipe', 'pipe'] })
  let pending = ''
  const show = data => {
    pending += data.toString('utf8')
    const lines = pending.split(/[\r\n]+/)
    pending = lines.pop()
    for (const line of lines) {
      if (!line.trim()) continue
      console.log(line)
      const match = line.match(/(?:Transferred:|,\s*)(\d{1,3})%/)
      if (!match) continue
      // `208.382 MiB / 8.501 GiB, 2%, 32.095 MiB/s, ETA 4m24s`
      const stats = line.match(/([\d.]+\s*(?:[kKMGTPE]i?(?:B|Bytes)?|B|Bytes)?)\s*\/\s*([\d.]+\s*(?:[kKMGTPE]i?(?:B|Bytes)?|B|Bytes)?),\s*\d{1,3}%,\s*([\d.]+\s*[kKMGTPE]?i?(?:B|Bytes)\/s)(?:,\s*ETA\s+([^\s,]+))?/)
      state(statePath, {
        phase: 'drive', status: 'downloading', progress: Math.min(100, Number(match[1])),
        ...(stats ? { done: stats[1], total: stats[2], speed: stats[3], eta: stats[4] && stats[4] !== '-' ? stats[4] : '' } : {}),
      })
    }
  }
  child.stdout.on('data', show)
  child.stderr.on('data', show)
  const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve) })
  if (pending.trim()) console.log(pending.trim())
  if (code !== 0) throw new Error(`rclone copyid failed with exit code ${code}`)
  const files = readdirSync(folder).filter(name => !name.endsWith('.part')).map(name => join(folder, name)).filter(name => statSync(name).isFile())
  if (files.length !== 1) throw new Error(`expected one downloaded Drive file, found ${files.length}`)
  const file = files[0]
  state(statePath, { phase: 'downloaded', status: 'downloaded', progress: 100, files: [file], video_files: videoExtensions.test(file) ? [file] : [], destination: file })
  console.log(`Drive download complete: ${file}`)
}

async function main() {
  if (process.argv.length !== 4) throw new Error('usage: drive_download.mjs CONFIG.json STATE.json')
  const [configPath, statePath] = process.argv.slice(2)
  const { file_id: fileId, resource_key: resourceKey, destination } = JSON.parse(readFileSync(configPath, 'utf8'))
  if (!/^[A-Za-z0-9_-]{10,128}$/.test(fileId) || (resourceKey && !/^[A-Za-z0-9_-]{1,128}$/.test(resourceKey))) throw new Error('invalid Drive file ID or resource key')
  loadEnv()
  if (process.env.BILI_RCLONE_REMOTE) {
    await downloadWithRclone(process.env.BILI_RCLONE_REMOTE, fileId, resourceKey, destination, statePath)
    return
  }
  const { google } = await import('googleapis')
  const token = await refreshToken()
  if (!token) throw new Error('Drive not connected — no refresh token')
  const auth = new google.auth.OAuth2(process.env.GOOGLE_CLIENT_ID, process.env.GOOGLE_CLIENT_SECRET, process.env.GOOGLE_REDIRECT_URI)
  auth.setCredentials({ refresh_token: token })
  const drive = google.drive({ version: 'v3', auth })
  const headers = resourceKey ? { 'X-Goog-Drive-Resource-Keys': `${fileId}/${resourceKey}` } : {}
  state(statePath, { kind: 'drive_download', phase: 'metadata', status: 'running', progress: 0 })
  const { data: file } = await drive.files.get({ fileId, fields: 'id,name,mimeType,size,capabilities(canDownload)', supportsAllDrives: true }, { headers })
  if (file.mimeType?.startsWith('application/vnd.google-apps.')) throw new Error('Google Docs/Sheets/Slides and folders cannot be downloaded as a video file')
  if (file.capabilities?.canDownload === false) throw new Error('Drive owner disabled downloading this file')
  const name = safeName(file.name)
  const folder = join(destination, fileId)
  mkdirSync(folder, { recursive: true })
  const finalPath = join(folder, name)
  const partPath = `${finalPath}.part`
  const total = Number(file.size) || 0
  if (existsSync(finalPath)) {
    if (total && statSync(finalPath).size === total) {
      state(statePath, { phase: 'downloaded', status: 'downloaded', progress: 100, files: [finalPath], video_files: videoExtensions.test(name) ? [finalPath] : [] })
      console.log(`Drive file already downloaded: ${finalPath}`)
      return
    }
    throw new Error(`destination already exists: ${finalPath}`)
  }
  const previous = existsSync(partPath) ? statSync(partPath).size : 0
  if (total && previous > total) throw new Error('partial file is larger than the Drive source')
  if (total && previous === total) {
    renameSync(partPath, finalPath)
    state(statePath, { phase: 'downloaded', status: 'downloaded', progress: 100, files: [finalPath], video_files: videoExtensions.test(name) ? [finalPath] : [] })
    return
  }
  console.log(`Drive download: ${name}${total ? ` (${total} bytes)` : ''}`)
  const requestHeaders = previous ? { ...headers, Range: `bytes=${previous}-` } : headers
  const response = await drive.files.get({ fileId, alt: 'media', supportsAllDrives: true }, { responseType: 'stream', headers: requestHeaders })
  const resumed = previous && response.status === 206
  if (resumed && !String(response.headers['content-range'] || '').startsWith(`bytes ${previous}-`)) throw new Error('Drive returned an unexpected resume range')
  let received = resumed ? previous : 0
  let lastLogged = 0
  let lastBytes = received
  const started = Date.now()
  const update = (force = false) => {
    const now = Date.now()
    if (!force && now - lastLogged < 1000) return
    const seconds = Math.max(1, (now - (lastLogged || started)) / 1000)
    const speed = Math.round((received - lastBytes) / seconds)
    const percent = total ? Math.min(100, Math.floor(received * 100 / total)) : 0
    console.log(`drive | ${percent}% | ${received}/${total || '?'} bytes | ${speed} B/s`)
    state(statePath, { phase: 'drive', status: 'downloading', progress: percent, downloaded_bytes: received, total_bytes: total, speed_bytes: speed })
    lastLogged = now
    lastBytes = received
  }
  response.data.on('data', chunk => { received += chunk.length; update() })
  await pipeline(response.data, createWriteStream(partPath, { flags: resumed ? 'a' : 'w' }))
  update(true)
  if (total && statSync(partPath).size !== total) throw new Error(`incomplete Drive download: ${statSync(partPath).size}/${total} bytes`)
  renameSync(partPath, finalPath)
  state(statePath, { phase: 'downloaded', status: 'downloaded', progress: 100, files: [finalPath], video_files: videoExtensions.test(name) ? [finalPath] : [], destination: finalPath })
  console.log(`Drive download complete: ${finalPath}`)
}

main().catch(error => { console.error(`Drive download failed: ${error.message}`); process.exitCode = 1 })
