const { createApp, nextTick } = Vue

const STATUS = {
  running: 'กำลังทำงาน', queued: 'รอคิว', paused: 'หยุดชั่วคราว', completed: 'เสร็จแล้ว', downloaded: 'เสร็จแล้ว',
  failed: 'ล้มเหลว', cancelled: 'ยกเลิกแล้ว', interrupted: 'ถูกขัดจังหวะ', stopped: 'หยุด',
}
const GROUP = {
  running: 'running', queued: 'waiting', paused: 'waiting', completed: 'done', downloaded: 'done',
  failed: 'failed', cancelled: 'failed', interrupted: 'failed', stopped: 'failed',
}
const FILTERS = { all: 'ทั้งหมด', running: 'กำลังทำงาน', waiting: 'รอคิว / หยุดไว้', done: 'เสร็จแล้ว', failed: 'ล้มเหลว / ยกเลิก' }
const KIND = { torrent: 'BitTorrent', drive_download: 'Google Drive', remote_download: 'Drive (rclone)', download: 'Bilibili', upload: 'อัปโหลด' }

// Log lines the workers print for live progress. The viewer folds them into a
// single "live" line so messages and errors are not buried under thousands of
// once-a-second updates.
const PROGRESS_LINE = [
  /^(torrent|drive|upload|hls|download|remote)\s*\|\s*\d{1,3}(\.\d+)?%/i,
  /\d{1,3}%,\s*[\d.]+\s*[kKMGTPE]?i?(?:B|Bytes)\/s/,
  /^\[#[0-9a-f]+ .*\]$/i,
  /^\S*\s*\d{1,3}(\.\d+)?%\s+\d{1,2}:\d{2}:\d{2}/,
  /^Transferred:/,
  /^\[[^\]]+\] task [\w-]+: [a-z_]+ \| \d{1,3}% \|/,
]
// Values Bili23's create_download accepts; "auto" follows Bili23's own
// priority list. Anything above 1080P (and Hi-Res/Dolby audio) needs a VIP login.
const BILI_QUALITIES = [
  ['auto', 'อัตโนมัติ (สูงสุดที่ได้)'], ['8K', '8K'], ['DOLBY_VISION', 'Dolby Vision'], ['HDR', 'HDR'],
  ['4K_SDR', '4K SDR เพิ่มคุณภาพ'], ['4K', '4K'], ['1080P60', '1080P 60fps'], ['1080P+', '1080P+ บิตเรตสูง'],
  ['1080P', '1080P'], ['720P', '720P'], ['480P', '480P'], ['360P', '360P'],
]
const BILI_CODECS = [['auto', 'อัตโนมัติ'], ['HEVC/H.265', 'HEVC / H.265'], ['AV1', 'AV1'], ['AVC/H.264', 'AVC / H.264 (เล่นได้ทุกที่)']]
const BILI_AUDIO = [['auto', 'อัตโนมัติ'], ['HI_RES', 'Hi-Res lossless'], ['DOLBY_ATMOS', 'Dolby Atmos'], ['192K', '192 kbps'], ['132K', '132 kbps'], ['64K', '64 kbps']]
const BILI_DEFAULTS = { quality: 'auto', codec: 'auto', audio_quality: 'auto', container: 'mp4', subtitle: false, redownload: false }
function savedBiliOptions() {
  try { return { ...BILI_DEFAULTS, ...JSON.parse(localStorage.getItem('bili-options') || '{}'), redownload: false } } catch { return { ...BILI_DEFAULTS } }
}

const MAX_LOG_LINES = 40000
const MAX_RENDERED = 4000

function classify(text) {
  if (text.startsWith('==>')) return /fail|cancel|could not|error/i.test(text) ? 'error' : 'event'
  if (PROGRESS_LINE.some(pattern => pattern.test(text))) return 'progress'
  if (/\b(error|errors|failed|failure|traceback|exception|fatal)\b/i.test(text) && !/\berrors?[:=]\s*0\b/i.test(text)) return 'error'
  if (/\bwarn(ing)?\b/i.test(text)) return 'warn'
  if (/^FILE:/.test(text)) return 'file'
  return 'text'
}

const encoder = new TextEncoder()
let lineId = 0
function toLines(text) {
  const parts = text.split('\n')
  if (parts.at(-1) === '') parts.pop()
  return parts.map(raw => {
    const line = raw.replace(/\r$/, '')
    return { id: ++lineId, text: line, kind: classify(line), bytes: encoder.encode(raw).length + 1 }
  })
}

createApp({
  data() { return {
    token: localStorage.getItem('bwt') || '', tokenDraft: '', editingToken: false,
    online: true, tokenInvalid: false, lastError: '', health: null,
    view: 'queue', source: 'torrent', toasts: [],
    driveLink: '', torrentSource: '', torrentData: '', torrentFileName: '', torrentFiles: [], selectedTorrent: [], torrentFilter: '',
    inspectionJob: '', inspectionPending: false,
    biliUrl: '', episodes: [], selectedEpisodes: [], parsing: false, biliOptions: savedBiliOptions(),
    BILI_QUALITIES, BILI_CODECS, BILI_AUDIO,
    files: [], selectedFiles: [], fileSearch: '', fileSort: 'size', filesLoadedAt: 0,
    jobs: [], jobFilter: 'all', laneFilter: 'all', jobSearch: '',
    logJob: '', logLines: [], logStart: 0, logOffset: 0, logSize: 0, logLoading: false, logLoadingOlder: false,
    logFollow: true, logHideProgress: true, logSearch: '', logRequest: 0,
    now: Date.now(), clockSkew: 0, healthAt: 0, timer: null,
  } },
  computed: {
    transferJobs() { return this.jobs.filter(job => job.lane !== 'convert' && job.kind !== 'torrent_inspect' && job.kind !== 'hls') },
    counts() {
      const counts = { running: 0, waiting: 0, done: 0, failed: 0 }
      for (const job of this.transferJobs) counts[GROUP[job.status] || 'failed']++
      return counts
    },
    filterLabel() { return FILTERS[this.jobFilter] },
    visibleJobs() {
      const search = this.jobSearch.trim().toLowerCase()
      return this.transferJobs.filter(job =>
        (this.jobFilter === 'all' || GROUP[job.status] === this.jobFilter) &&
        (this.laneFilter === 'all' || this.laneOf(job) === this.laneFilter) &&
        (!search || `${this.jobLabel(job)} ${job.url} ${job.job}`.toLowerCase().includes(search)))
    },
    allTorrentSelected() { return this.torrentFiles.length > 0 && this.selectedTorrent.length === this.torrentFiles.length },
    selectedTorrentSize() {
      const chosen = new Set(this.selectedTorrent)
      return this.torrentFiles.reduce((sum, file) => sum + (chosen.has(file.index) ? file.size : 0), 0)
    },
    visibleTorrentFiles() {
      const search = this.torrentFilter.trim().toLowerCase()
      return search ? this.torrentFiles.filter(file => file.path.toLowerCase().includes(search)) : this.torrentFiles
    },
    visibleFiles() {
      const search = this.fileSearch.trim().toLowerCase()
      const rows = search ? this.files.filter(file => file.relative.toLowerCase().includes(search)) : [...this.files]
      const sorters = {
        size: (a, b) => b.size - a.size,
        modified: (a, b) => (b.modified || 0) - (a.modified || 0),
        name: (a, b) => a.relative.localeCompare(b.relative, undefined, { numeric: true }),
      }
      return rows.sort(sorters[this.fileSort])
    },
    allFilesSelected() { return this.visibleFiles.length > 0 && this.visibleFiles.every(file => this.selectedFiles.includes(file.path)) },
    selectedFilesSize() {
      const chosen = new Set(this.selectedFiles)
      return this.files.reduce((sum, file) => sum + (chosen.has(file.path) ? file.size : 0), 0)
    },
    logJobRow() { return this.jobs.find(job => job.job === this.logJob) },
    logProgress() {
      for (let i = this.logLines.length - 1; i >= 0; i--) if (this.logLines[i].kind === 'progress') return this.logLines[i].text
      return ''
    },
    logFiltered() {
      const search = this.logSearch.trim().toLowerCase()
      if (search) return this.logLines.filter(line => line.text.toLowerCase().includes(search))
      return this.logHideProgress ? this.logLines.filter(line => line.kind !== 'progress') : this.logLines
    },
    biliOptionHint() {
      const o = this.biliOptions
      if (['DOLBY_VISION', 'HDR'].includes(o.quality) && o.codec === 'AVC/H.264') return 'HDR และ Dolby Vision ไม่มีใน H.264 ถ้าเลือกแบบนี้ Bili23 จะใช้ codec อื่นแทน'
      if (o.audio_quality === 'HI_RES' && o.container === 'mp4') return 'เสียง Hi-Res เป็น FLAC ถ้าจะเก็บ FLAC ไว้ครบ ควรเลือกไฟล์ MKV'
      if (!['auto', '1080P', '720P', '480P', '360P'].includes(o.quality)) return 'คุณภาพสูงกว่า 1080P ต้องล็อกอินบัญชี VIP ถ้าไม่ได้ล็อกอิน Bili23 จะลดคุณภาพลงเอง'
      return ''
    },
    logHiddenCount() { return this.logLines.length - this.logFiltered.length },
    logVisible() { return this.logFiltered.length > MAX_RENDERED ? this.logFiltered.slice(-MAX_RENDERED) : this.logFiltered },
  },
  watch: {
    view(value) { if (value === 'files') this.loadFiles() },
    torrentSource() { this.resetTorrent() },
    biliOptions: { deep: true, handler(value) { try { localStorage.setItem('bili-options', JSON.stringify({ ...value, redownload: false })) } catch { /* private mode */ } } },
    logHideProgress() { this.scrollLogIfFollowing() },
    logSearch() { this.scrollLogIfFollowing() },
  },
  methods: {
    // --- plumbing -----------------------------------------------------------
    async api(path, body = {}) {
      if (!this.token) throw Error('ใส่ Access token แล้วกดบันทึกก่อน')
      let response
      try {
        response = await fetch(path, {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Token': this.token },
          body: JSON.stringify(body),
        })
      } catch (error) {
        this.online = false
        this.lastError = 'ติดต่อเซิร์ฟเวอร์ไม่ได้'
        throw Error('ติดต่อเซิร์ฟเวอร์ไม่ได้ ตรวจว่าเว็บเซิร์ฟเวอร์ยังทำงานอยู่')
      }
      this.online = true
      const data = await response.json().catch(() => ({}))
      this.tokenInvalid = response.status === 401
      if (this.tokenInvalid) {
        this.editingToken = true
        throw Error('Access token ไม่ถูกต้อง (token เปลี่ยนทุกครั้งที่รีสตาร์ตเซิร์ฟเวอร์ ถ้ารันเซลล์ server ใหม่ต้องใส่ token ใหม่)')
      }
      if (!response.ok || data.error) throw Error(data.error || `HTTP ${response.status}`)
      return data
    },
    notify(text, kind = 'info') {
      const id = Date.now() + Math.random()
      this.toasts.push({ id, text, kind })
      if (this.toasts.length > 4) this.toasts.shift()
      setTimeout(() => this.dismiss(id), kind === 'error' ? 9000 : 4500)
    },
    fail(error) { this.notify(error.message || String(error), 'error') },
    dismiss(id) { this.toasts = this.toasts.filter(toast => toast.id !== id) },
    editToken() { this.tokenDraft = ''; this.editingToken = true },
    saveToken() {
      this.token = this.tokenDraft.trim()
      this.tokenDraft = ''
      this.editingToken = false
      if (this.token) localStorage.setItem('bwt', this.token); else localStorage.removeItem('bwt')
      this.refresh(true)
    },

    // --- formatting ---------------------------------------------------------
    size(bytes) {
      let n = +bytes || 0; const units = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0
      while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
      return `${n.toFixed(i ? 1 : 0)} ${units[i]}`
    },
    duration(seconds) {
      seconds = Math.max(0, Math.round(seconds))
      const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60
      return h ? `${h} ชม. ${m} นาที` : m ? `${m} นาที ${s} วิ` : `${s} วิ`
    },
    clock(seconds) {
      seconds = Math.round(+seconds || 0)
      const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = String(seconds % 60).padStart(2, '0')
      return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`
    },
    ago(epoch) {
      const seconds = (this.now / 1000) - epoch
      if (seconds < 60) return 'เมื่อสักครู่'
      if (seconds < 3600) return `${Math.floor(seconds / 60)} นาทีที่แล้ว`
      if (seconds < 86400) return `${Math.floor(seconds / 3600)} ชม.ที่แล้ว`
      return new Date(epoch * 1000).toLocaleDateString('th-TH', { day: 'numeric', month: 'short' })
    },
    basename(path) { return String(path).split(/[\\/]/).pop() },
    dirname(path) { const parts = String(path).split(/[\\/]/); parts.pop(); return parts.join('/') },
    statusLabel(status) { return STATUS[status] || status },
    laneOf(job) { return job.lane === 'upload' || job.kind === 'upload' ? 'upload' : 'download' },
    kindLabel(job) { return KIND[job.kind] || job.kind || job.lane },
    jobLabel(job) {
      if (job.details?.title) return job.details.title
      const value = String(job.url || job.job)
      if (value.startsWith('magnet:')) {
        const name = new URLSearchParams(value.slice(value.indexOf('?') + 1)).get('dn')
        return name || 'Magnet link'
      }
      const drive = value.match(/drive\.google\.com\/.*?(?:\/d\/|[?&]id=)([\w-]+)/)
      if (drive) return `Drive file ${drive[1]}`
      // Drop ?query / #hash first: tracking parameters are not a name.
      const path = value.split(/[?#]/)[0]
      try { return decodeURIComponent(path.split(/[/\\]/).filter(Boolean).pop() || job.job) } catch { return path }
    },
    showPercent(job) { return ['running', 'paused'].includes(job.status) || (+job.progress > 0 && GROUP[job.status] !== 'done') },
    jobFacts(job) {
      const d = job.details || {}
      const facts = []
      if (job.status === 'running' || job.status === 'paused') {
        if (d.done && d.total) facts.push(`${d.done} / ${d.total}`)
        else if (d.downloaded_bytes) facts.push(`${this.size(d.downloaded_bytes)} / ${d.total_bytes ? this.size(d.total_bytes) : '?'}`)
        if (job.status === 'running') {
          const arrow = this.laneOf(job) === 'upload' ? '↑' : '↓'
          if (d.speed) facts.push(`${arrow} ${d.speed}`)
          else if (d.speed_bytes) facts.push(`${arrow} ${this.size(d.speed_bytes)}/s`)
          if (d.eta) facts.push(`เหลือ ${d.eta}`)
          if (d.peers !== undefined && job.kind === 'torrent') facts.push(`${d.peers} peers`)
        }
        if (d.file_count > 1) facts.push(`ไฟล์ ${d.file_index} / ${d.file_count}`)
        if (d.current_file && this.laneOf(job) === 'upload') facts.push(this.basename(d.current_file))
      }
      if (job.started_at) {
        const end = job.finished_at || (job.status === 'running' ? this.now / 1000 : null)
        if (end) facts.push(`ใช้เวลา ${this.duration(end - job.started_at)}`)
      }
      facts.push(job.finished_at ? `จบ ${this.ago(job.finished_at)}` : `สร้าง ${String(job.created || '').replace(' UTC', '')}`)
      return facts
    },
    jobError(job) {
      if (GROUP[job.status] !== 'failed') return ''
      const d = job.details || {}
      if (d.error) return d.error
      if (job.status === 'interrupted') return 'เว็บเซิร์ฟเวอร์รีสตาร์ตระหว่างทำงาน กด “ลองใหม่” เพื่อทำต่อ'
      if (d.exit_code) return `โปรแกรมจบด้วย exit code ${d.exit_code} ดูรายละเอียดใน Log`
      return ''
    },
    setFilter(value) { this.jobFilter = this.jobFilter === value ? 'all' : value; this.view = 'queue' },

    // --- torrent ------------------------------------------------------------
    resetTorrent() { this.torrentFiles = []; this.selectedTorrent = []; this.torrentFilter = ''; this.inspectionJob = ''; this.inspectionPending = false },
    clearTorrentFile() { this.torrentData = ''; this.torrentFileName = ''; this.resetTorrent(); if (this.$refs.torrentInput) this.$refs.torrentInput.value = '' },
    async chooseTorrent(event) {
      const file = event.target.files?.[0]
      this.torrentData = ''
      this.torrentFileName = ''
      this.resetTorrent()
      if (!file) return
      if (file.size > 4 * 1024 * 1024) { this.notify('ไฟล์ .torrent ต้องไม่เกิน 4 MB', 'error'); event.target.value = ''; return }
      const bytes = new Uint8Array(await file.arrayBuffer())
      let binary = ''
      for (let i = 0; i < bytes.length; i += 32768) binary += String.fromCharCode(...bytes.subarray(i, i + 32768))
      this.torrentData = btoa(binary)
      this.torrentFileName = file.name
    },
    async inspectTorrent() {
      if (this.inspectionPending || (!this.torrentSource.trim() && !this.torrentData)) return
      try {
        const result = await this.api('/api/torrent/inspect', { source: this.torrentSource.trim(), torrent_data: this.torrentData })
        this.resetTorrent()
        this.inspectionJob = result.job
        this.inspectionPending = true
      } catch (error) { this.fail(error) }
    },
    async checkInspection() {
      if (!this.inspectionJob || !this.inspectionPending) return
      try {
        const result = await this.api('/api/status', { job: this.inspectionJob })
        if (result.running) return
        this.inspectionPending = false
        if (result.state?.phase !== 'ready') throw Error(result.state?.error || 'อ่าน torrent ไม่สำเร็จ')
        this.torrentFiles = result.state.torrent_files || []
        this.selectedTorrent = this.torrentFiles.map(file => file.index)
      } catch (error) { this.inspectionPending = false; this.fail(error) }
    },
    toggleAllTorrent(checked) { this.selectedTorrent = checked ? this.torrentFiles.map(file => file.index) : [] },
    async submitTorrent() {
      if (!this.inspectionJob) { this.notify('อ่านรายการ torrent ใหม่ก่อน', 'error'); return }
      try {
        const result = await this.api('/api/torrent', { inspection_job: this.inspectionJob, selected_files: this.selectedTorrent })
        this.queued(result.job)
        this.torrentSource = ''; this.torrentData = ''; this.torrentFileName = ''; this.resetTorrent()
      } catch (error) { this.fail(error) }
    },

    // --- drive / bilibili ---------------------------------------------------
    async submitDrive() {
      if (!this.driveLink.trim()) return
      try { this.queued((await this.api('/api/drive/download', { source: this.driveLink.trim() })).job); this.driveLink = '' } catch (error) { this.fail(error) }
    },
    async parseBili() {
      if (!this.biliUrl.trim()) return
      this.parsing = true
      try {
        this.episodes = await this.api('/api/parse', { url: this.biliUrl.trim() })
        this.selectedEpisodes = this.episodes.filter(item => !item.needs_reparse).map(item => item.episode_id)
      } catch (error) { this.fail(error) } finally { this.parsing = false }
    },
    async submitBili() {
      try {
        this.queued((await this.api('/api/pull', { url: this.biliUrl.trim(), episode_ids: this.selectedEpisodes, ...this.biliOptions })).job)
        this.episodes = []; this.selectedEpisodes = []
      } catch (error) { this.fail(error) }
    },
    queued(job) {
      this.notify('เพิ่มงานเข้าคิวแล้ว', 'ok')
      this.jobFilter = 'all'
      if (window.matchMedia('(max-width: 900px)').matches) this.view = 'queue'
      this.loadJobs().then(() => { const row = this.jobs.find(item => item.job === job); if (row) this.openLog(row) })
    },

    // --- files --------------------------------------------------------------
    async loadFiles() {
      if (!this.token) return
      try {
        this.files = await this.api('/api/files/downloads')
        this.filesLoadedAt = Date.now()
        const present = new Set(this.files.map(file => file.path))
        this.selectedFiles = this.selectedFiles.filter(path => present.has(path))
      } catch (error) { this.fail(error) }
    },
    toggleAllFiles(checked) {
      const visible = new Set(this.visibleFiles.map(file => file.path))
      const others = this.selectedFiles.filter(path => !visible.has(path))
      this.selectedFiles = checked ? [...others, ...visible] : others
    },
    async upload() {
      try {
        await this.api('/api/upload', { files: this.selectedFiles })
        this.notify(`เพิ่มงานอัปโหลด ${this.selectedFiles.length} ไฟล์แล้ว`, 'ok')
        this.selectedFiles = []
        this.view = 'queue'
        this.loadJobs()
      } catch (error) { this.fail(error) }
    },
    async deleteFiles() {
      const paths = [...this.selectedFiles]
      if (!paths.length || !confirm(`ลบไฟล์ที่เลือก ${paths.length} ไฟล์ (${this.size(this.selectedFilesSize)}) ออกจากเครื่องถาวรหรือไม่?`)) return
      try {
        const result = await this.api('/api/delete/downloads', { paths })
        this.selectedFiles = []
        await this.loadFiles()
        this.notify(`ลบแล้ว ${result.deleted} ไฟล์${result.failed ? ` · ลบไม่ได้ ${result.failed} ไฟล์ (อาจมีงานกำลังใช้อยู่)` : ''}`, result.failed ? 'error' : 'ok')
      } catch (error) { this.fail(error) }
    },

    // --- queue --------------------------------------------------------------
    async loadJobs() {
      if (!this.token) return
      try {
        const rows = await this.api('/api/jobs')
        // Durations mix server timestamps with "now"; measure on the server's clock.
        if (rows.length && rows[0].now) { this.clockSkew = rows[0].now * 1000 - Date.now(); this.now = Date.now() + this.clockSkew }
        this.jobs = rows.sort((a, b) =>
          (GROUP[a.status] === 'running' ? 0 : GROUP[a.status] === 'waiting' ? 1 : 2) - (GROUP[b.status] === 'running' ? 0 : GROUP[b.status] === 'waiting' ? 1 : 2) ||
          String(b.created).localeCompare(String(a.created)) || b.job.localeCompare(a.job))
      } catch (error) { this.lastError = error.message; throw error }
    },
    async action(path, job, done) {
      try { await this.api(path, { job }); await this.loadJobs(); if (done) this.notify(done, 'ok') } catch (error) { this.fail(error) }
    },
    pause(job) { return this.action('/api/pause', job) },
    resume(job) { return this.action('/api/resume', job) },
    async cancel(job) { if (confirm('ยกเลิกงานนี้หรือไม่?')) await this.action('/api/cancel', job, 'ยกเลิกงานแล้ว') },
    async retry(job) {
      if (!confirm('เพิ่มงานนี้เข้าคิวใหม่หรือไม่?')) return
      try { const result = await this.api('/api/retry', { job }); this.notify('เพิ่มงานเข้าคิวใหม่แล้ว', 'ok'); await this.loadJobs(); if (this.logJob === job && result.job) this.openLog({ job: result.job }) } catch (error) { this.fail(error) }
    },

    // --- log viewer ---------------------------------------------------------
    async openLog(job) {
      const id = job.job
      this.logJob = id
      this.logLines = []; this.logStart = 0; this.logOffset = 0; this.logSize = 0
      this.logSearch = ''; this.logFollow = true
      this.logLoading = true
      const request = ++this.logRequest
      try {
        const result = await this.api('/api/log', { job: id })
        if (request !== this.logRequest) return
        this.logLines = toLines(result.text)
        this.logStart = result.start; this.logOffset = result.offset; this.logSize = result.size
        this.scrollLogIfFollowing()
      } catch (error) { if (request === this.logRequest) this.fail(error) } finally { if (request === this.logRequest) this.logLoading = false }
    },
    async pollLog() {
      if (!this.logJob || this.logLoading) return
      const id = this.logJob, request = ++this.logRequest
      try {
        const result = await this.api('/api/log', { job: id, offset: this.logOffset })
        if (request !== this.logRequest || id !== this.logJob) return
        if (result.reset) return this.openLog({ job: id })
        this.logSize = result.size
        if (!result.text) return
        this.logOffset = result.offset
        const lines = this.logLines.concat(toLines(result.text))
        if (lines.length > MAX_LOG_LINES) {
          const dropped = lines.splice(0, lines.length - MAX_LOG_LINES)
          this.logStart += dropped.reduce((sum, line) => sum + line.bytes, 0)
        }
        this.logLines = lines
        this.scrollLogIfFollowing()
      } catch { /* the queue refresh reports connection errors */ }
    },
    async loadOlderLog() {
      const id = this.logJob
      this.logLoadingOlder = true
      try {
        const body = this.$refs.logBody
        const fromBottom = body ? body.scrollHeight - body.scrollTop : 0
        const result = await this.api('/api/log', { job: id, before: this.logStart })
        if (id !== this.logJob) return
        this.logLines = toLines(result.text).concat(this.logLines)
        this.logStart = result.start
        this.logFollow = false
        await nextTick()
        if (body) body.scrollTop = body.scrollHeight - fromBottom
      } catch (error) { this.fail(error) } finally { this.logLoadingOlder = false }
    },
    closeLog() { this.logJob = ''; this.logLines = []; this.logRequest++ },
    onLogScroll() {
      const body = this.$refs.logBody
      if (!body) return
      const atEnd = body.scrollHeight - body.scrollTop - body.clientHeight < 24
      if (atEnd !== this.logFollow) this.logFollow = atEnd
    },
    async scrollLogIfFollowing() {
      if (!this.logFollow) return
      await nextTick()
      const body = this.$refs.logBody
      if (body) body.scrollTop = body.scrollHeight
    },
    jumpToEnd() { this.logFollow = true; this.scrollLogIfFollowing() },
    async copyLog() {
      try { await navigator.clipboard.writeText(this.logFiltered.map(line => line.text).join('\n')); this.notify('คัดลอก log แล้ว', 'ok') } catch (error) { this.fail(error) }
    },
    async downloadLog() {
      try {
        const result = await this.api('/api/log', { job: this.logJob, full: true })
        const link = document.createElement('a')
        link.href = URL.createObjectURL(new Blob([result.text], { type: 'text/plain' }))
        link.download = `${this.logJob}.log`
        link.click()
        setTimeout(() => URL.revokeObjectURL(link.href), 1000)
      } catch (error) { this.fail(error) }
    },

    // --- refresh loop -------------------------------------------------------
    async refresh(force = false) {
      this.now = Date.now() + this.clockSkew
      if (!this.token) return
      try {
        await this.loadJobs()
        if (force || this.now - this.healthAt > 30000) { this.healthAt = this.now; this.health = await this.api('/api/health') }
      } catch (error) { if (force) this.fail(error) }
      if (this.view === 'files' && Date.now() - this.filesLoadedAt > 10000) this.loadFiles()
      if (this.inspectionPending) this.checkInspection()
      if (this.logJob) this.pollLog()
    },
    onKey(event) { if (event.key === 'Escape' && this.logJob) this.closeLog() },
  },
  mounted() {
    this.refresh(true)
    this.timer = setInterval(() => this.refresh(), 2000)
    window.addEventListener('keydown', this.onKey)
  },
  beforeUnmount() { clearInterval(this.timer); window.removeEventListener('keydown', this.onKey) },
}).component('bili-account', BiliAccount).mount('#transfer-app')
