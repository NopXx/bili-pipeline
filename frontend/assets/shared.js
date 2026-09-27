// Pieces shared by the Studio page (index.html) and the Transfer page:
// formatting helpers, job/status vocabulary, the job card and the log drawer.
// Plain script (no bundler): everything hangs off the global `Shared`.
const Shared = (() => {
  const { nextTick } = Vue

  const STATUS = {
    running: 'กำลังทำงาน', queued: 'รอคิว', paused: 'หยุดชั่วคราว', completed: 'เสร็จแล้ว', downloaded: 'เสร็จแล้ว',
    failed: 'ล้มเหลว', cancelled: 'ยกเลิกแล้ว', interrupted: 'ถูกขัดจังหวะ', stopped: 'หยุด',
  }
  const GROUP = {
    running: 'running', queued: 'waiting', paused: 'waiting', completed: 'done', downloaded: 'done',
    failed: 'failed', cancelled: 'failed', interrupted: 'failed', stopped: 'failed',
  }
  const FILTERS = { all: 'ทั้งหมด', running: 'กำลังทำงาน', waiting: 'รอคิว / หยุดไว้', done: 'เสร็จแล้ว', failed: 'ล้มเหลว / ยกเลิก' }
  const KIND = {
    torrent: 'BitTorrent', drive_download: 'Google Drive', remote_download: 'คลัง Drive', download: 'Bilibili',
    upload: 'อัปโหลด', hls: 'แปลง HLS', torrent_inspect: 'อ่าน torrent',
  }
  const ACTIVE = ['queued', 'running', 'paused']
  const RETRYABLE = ['failed', 'interrupted', 'cancelled', 'stopped']

  // Values Bili23's create_download accepts; "auto" follows Bili23's own
  // priority list. Anything above 1080P (and Hi-Res/Dolby audio) needs a VIP login.
  const BILI_QUALITIES = [
    ['auto', 'อัตโนมัติ (สูงสุดที่ได้)'], ['8K', '8K'], ['DOLBY_VISION', 'Dolby Vision'], ['HDR', 'HDR'],
    ['4K_SDR', '4K SDR เพิ่มคุณภาพ'], ['4K', '4K'], ['1080P60', '1080P 60fps'], ['1080P+', '1080P+ บิตเรตสูง'],
    ['1080P', '1080P'], ['720P', '720P'], ['480P', '480P'], ['360P', '360P'],
  ]
  const BILI_CODECS = [['auto', 'อัตโนมัติ'], ['HEVC/H.265', 'HEVC / H.265'], ['AV1', 'AV1'], ['AVC/H.264', 'AVC / H.264 (เล่นได้ทุกที่)']]
  const BILI_AUDIO = [['auto', 'อัตโนมัติ'], ['HI_RES', 'Hi-Res lossless'], ['DOLBY_ATMOS', 'Dolby Atmos'], ['192K', '192 kbps'], ['132K', '132 kbps'], ['64K', '64 kbps']]
  function biliOptionHint(o) {
    if (['DOLBY_VISION', 'HDR'].includes(o.quality) && o.codec === 'AVC/H.264') return 'HDR และ Dolby Vision ไม่มีใน H.264 ถ้าเลือกแบบนี้ Bili23 จะใช้ codec อื่นแทน'
    if (o.audio_quality === 'HI_RES' && o.container === 'mp4') return 'เสียง Hi-Res เป็น FLAC ถ้าจะเก็บ FLAC ไว้ครบ ควรเลือกไฟล์ MKV'
    if (!['auto', '1080P', '720P', '480P', '360P'].includes(o.quality)) return 'คุณภาพสูงกว่า 1080P ต้องล็อกอินบัญชี VIP ถ้าไม่ได้ล็อกอิน Bili23 จะลดคุณภาพลงเอง'
    return ''
  }

  // localStorage can throw (private mode, blocked storage); never let it break the page.
  function load(key, fallback) {
    try { const value = JSON.parse(localStorage.getItem(key) || 'null'); return value ?? fallback } catch { return fallback }
  }
  function save(key, value) { try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* ignore */ } }

  // --- formatting -----------------------------------------------------------
  function size(bytes) {
    let n = +bytes || 0; const units = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
    return `${n.toFixed(i ? 1 : 0)} ${units[i]}`
  }
  function duration(seconds) {
    seconds = Math.max(0, Math.round(+seconds || 0))
    const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60
    return h ? `${h} ชม. ${m} นาที` : m ? `${m} นาที ${s} วิ` : `${s} วิ`
  }
  function clock(seconds) {
    seconds = Math.round(+seconds || 0)
    const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = String(seconds % 60).padStart(2, '0')
    return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`
  }
  function ago(epoch, nowMs) {
    const seconds = nowMs / 1000 - epoch
    if (seconds < 60) return 'เมื่อสักครู่'
    if (seconds < 3600) return `${Math.floor(seconds / 60)} นาทีที่แล้ว`
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} ชม.ที่แล้ว`
    return new Date(epoch * 1000).toLocaleDateString('th-TH', { day: 'numeric', month: 'short' })
  }
  function bitrate(bps) { return bps ? `${(+bps / 1e6).toFixed(2)} Mbps` : 'ไม่ทราบ' }
  function basename(path) { return String(path).split(/[\\/]/).pop() }
  function dirname(path) { const parts = String(path).split(/[\\/]/); parts.pop(); return parts.join('/') }

  // --- jobs -----------------------------------------------------------------
  function statusLabel(status) { return STATUS[status] || status }
  function laneOf(job) {
    if (job.lane === 'upload' || job.kind === 'upload') return 'upload'
    if (job.lane === 'convert' || job.kind === 'hls') return 'convert'
    return 'download'
  }
  function kindLabel(job) { return KIND[job.kind] || job.kind || job.lane }
  function jobLabel(job) {
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
  }
  function showPercent(job) { return ['running', 'paused'].includes(job.status) || (+job.progress > 0 && GROUP[job.status] !== 'done') }
  function jobFacts(job, nowMs) {
    const d = job.details || {}
    const facts = []
    if (job.status === 'running' || job.status === 'paused') {
      if (d.done && d.total) facts.push(`${d.done} / ${d.total}`)
      else if (d.downloaded_bytes) facts.push(`${size(d.downloaded_bytes)} / ${d.total_bytes ? size(d.total_bytes) : '?'}`)
      if (job.status === 'running') {
        const arrow = laneOf(job) === 'upload' ? '↑' : '↓'
        if (d.speed) facts.push(`${arrow} ${d.speed}`)
        else if (d.speed_bytes) facts.push(`${arrow} ${size(d.speed_bytes)}/s`)
        if (d.eta) facts.push(`เหลือ ${d.eta}`)
        else if (typeof d.eta_seconds === 'number') facts.push(`เหลือ ${duration(d.eta_seconds)}`)
        if (d.peers !== undefined && job.kind === 'torrent') facts.push(`${d.peers} peers`)
      }
      if (d.file_count > 1) facts.push(`ไฟล์ ${d.file_index} / ${d.file_count}`)
      if (d.current_file && laneOf(job) !== 'download') facts.push(basename(d.current_file))
    }
    if (job.started_at) {
      const end = job.finished_at || (job.status === 'running' ? nowMs / 1000 : null)
      if (end) facts.push(`ใช้เวลา ${duration(end - job.started_at)}`)
    }
    facts.push(job.finished_at ? `จบ ${ago(job.finished_at, nowMs)}` : `สร้าง ${String(job.created || '').replace(' UTC', '')}`)
    return facts
  }
  function jobError(job) {
    if (GROUP[job.status] !== 'failed') return ''
    const d = job.details || {}
    if (d.error) return d.error
    if (job.status === 'interrupted') return 'เว็บเซิร์ฟเวอร์รีสตาร์ตระหว่างทำงาน กด “ลองใหม่” เพื่อทำต่อ'
    if (d.exit_code) return `โปรแกรมจบด้วย exit code ${d.exit_code} ดูรายละเอียดใน Log`
    return ''
  }
  // Running first, then waiting, then history; newest first inside each group.
  function sortJobs(rows) {
    const rank = job => ({ running: 0, waiting: 1 })[GROUP[job.status]] ?? 2
    return rows.sort((a, b) => rank(a) - rank(b) || String(b.created).localeCompare(String(a.created)) || b.job.localeCompare(a.job))
  }

  // --- log lines --------------------------------------------------------------
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

  // --- <job-card> -------------------------------------------------------------
  const JobCard = {
    props: { job: { type: Object, required: true }, now: { type: Number, required: true }, selected: Boolean },
    emits: ['log', 'pause', 'resume', 'cancel', 'retry'],
    computed: {
      lane() { return laneOf(this.job) },
      label() { return jobLabel(this.job) },
      facts() { return jobFacts(this.job, this.now) },
      error() { return jobError(this.job) },
      percent() { return showPercent(this.job) },
      // The queue only pauses downloads and uploads; a conversion keeps its GPU.
      pausable() { return this.lane !== 'convert' && ['queued', 'running'].includes(this.job.status) },
    },
    methods: { statusLabel, kindLabel },
    template: `
      <article class="job" :class="['s-' + job.status, {selected}]">
        <div class="job-head">
          <span class="badge" :class="'s-' + job.status">{{statusLabel(job.status)}}</span>
          <span class="kind">{{kindLabel(job)}}</span>
          <span class="chip-gpu" v-if="job.gpu !== null && job.gpu !== undefined && lane === 'convert'">GPU {{job.gpu}}</span>
          <b class="job-name" :title="job.url || job.job">{{label}}</b>
          <span class="pct" v-if="percent">{{Math.floor(+job.progress || 0)}}%</span>
        </div>
        <div class="bar-track" v-if="percent || job.status === 'running'"><div class="bar-fill" :class="{live: job.status === 'running'}" :style="{width: (+job.progress || 0) + '%'}"></div></div>
        <div class="job-meta"><span v-for="part in facts" :key="part">{{part}}</span></div>
        <div class="job-error" v-if="error">{{error}}</div>
        <div class="job-actions">
          <button class="ghost small" @click="$emit('log', job)">Log</button>
          <button v-if="pausable" class="ghost small" @click="$emit('pause', job.job)">หยุดชั่วคราว</button>
          <button v-if="job.status === 'paused'" class="small" @click="$emit('resume', job.job)">ทำต่อ</button>
          <button v-if="['queued','running','paused'].includes(job.status)" class="danger small" @click="$emit('cancel', job.job)">ยกเลิก</button>
          <button v-if="['failed','interrupted','cancelled','stopped'].includes(job.status)" class="small" @click="$emit('retry', job.job)">ลองใหม่</button>
          <slot></slot>
          <span class="job-id">{{job.job}}</span>
        </div>
      </article>`,
  }

  // --- <log-drawer> -----------------------------------------------------------
  // Reads /api/log by byte range: the tail first, then only what was appended,
  // with "older lines" paging back. Progress lines fold into one live line.
  const LogDrawer = {
    props: { jobId: { type: String, required: true }, row: Object, api: { type: Function, required: true } },
    emits: ['close', 'notify'],
    data() { return {
      lines: [], start: 0, offset: 0, bytes: 0, loading: false, loadingOlder: false,
      follow: true, hideProgress: true, search: '', request: 0, timer: null,
    } },
    computed: {
      progress() {
        for (let i = this.lines.length - 1; i >= 0; i--) if (this.lines[i].kind === 'progress') return this.lines[i].text
        return ''
      },
      filtered() {
        const search = this.search.trim().toLowerCase()
        if (search) return this.lines.filter(line => line.text.toLowerCase().includes(search))
        return this.hideProgress ? this.lines.filter(line => line.kind !== 'progress') : this.lines
      },
      hiddenCount() { return this.lines.length - this.filtered.length },
      visible() { return this.filtered.length > MAX_RENDERED ? this.filtered.slice(-MAX_RENDERED) : this.filtered },
      status() { return this.row?.status || 'stopped' },
    },
    watch: {
      jobId() { this.open() },
      hideProgress() { this.scrollIfFollowing() },
      search() { this.scrollIfFollowing() },
    },
    methods: {
      size, statusLabel, jobLabel,
      fail(error) { this.$emit('notify', error.message || String(error), 'error') },
      async open() {
        const id = this.jobId
        this.lines = []; this.start = 0; this.offset = 0; this.bytes = 0
        this.search = ''; this.follow = true; this.loading = true
        const request = ++this.request
        try {
          const result = await this.api('/api/log', { job: id })
          if (request !== this.request) return
          this.lines = toLines(result.text)
          this.start = result.start; this.offset = result.offset; this.bytes = result.size
          this.scrollIfFollowing()
        } catch (error) { if (request === this.request) this.fail(error) } finally { if (request === this.request) this.loading = false }
      },
      async poll() {
        if (this.loading) return
        const id = this.jobId, request = ++this.request
        try {
          const result = await this.api('/api/log', { job: id, offset: this.offset })
          if (request !== this.request || id !== this.jobId) return
          if (result.reset) return this.open()
          this.bytes = result.size
          if (!result.text) return
          this.offset = result.offset
          const lines = this.lines.concat(toLines(result.text))
          if (lines.length > MAX_LOG_LINES) {
            const dropped = lines.splice(0, lines.length - MAX_LOG_LINES)
            this.start += dropped.reduce((sum, line) => sum + line.bytes, 0)
          }
          this.lines = lines
          this.scrollIfFollowing()
        } catch { /* the page's refresh loop reports connection errors */ }
      },
      async loadOlder() {
        const id = this.jobId
        this.loadingOlder = true
        try {
          const body = this.$refs.body
          const fromBottom = body ? body.scrollHeight - body.scrollTop : 0
          const result = await this.api('/api/log', { job: id, before: this.start })
          if (id !== this.jobId) return
          this.lines = toLines(result.text).concat(this.lines)
          this.start = result.start
          this.follow = false
          await nextTick()
          if (body) body.scrollTop = body.scrollHeight - fromBottom
        } catch (error) { this.fail(error) } finally { this.loadingOlder = false }
      },
      onScroll() {
        const body = this.$refs.body
        if (!body) return
        const atEnd = body.scrollHeight - body.scrollTop - body.clientHeight < 24
        if (atEnd !== this.follow) this.follow = atEnd
      },
      async scrollIfFollowing() {
        if (!this.follow) return
        await nextTick()
        const body = this.$refs.body
        if (body) body.scrollTop = body.scrollHeight
      },
      jumpToEnd() { this.follow = true; this.scrollIfFollowing() },
      async copy() {
        try { await navigator.clipboard.writeText(this.filtered.map(line => line.text).join('\n')); this.$emit('notify', 'คัดลอก log แล้ว', 'ok') } catch (error) { this.fail(error) }
      },
      async download() {
        try {
          const result = await this.api('/api/log', { job: this.jobId, full: true })
          const link = document.createElement('a')
          link.href = URL.createObjectURL(new Blob([result.text], { type: 'text/plain' }))
          link.download = `${this.jobId}.log`
          link.click()
          setTimeout(() => URL.revokeObjectURL(link.href), 1000)
        } catch (error) { this.fail(error) }
      },
      onKey(event) { if (event.key === 'Escape') this.$emit('close') },
    },
    mounted() {
      this.open()
      this.timer = setInterval(() => this.poll(), 2000)
      window.addEventListener('keydown', this.onKey)
    },
    beforeUnmount() { clearInterval(this.timer); this.request++; window.removeEventListener('keydown', this.onKey) },
    template: `
      <div class="scrim" @click="$emit('close')"></div>
      <aside class="log-drawer" role="dialog" aria-label="Job log">
        <header class="log-head">
          <div class="log-title">
            <span class="badge" :class="'s-' + status">{{statusLabel(status)}}</span>
            <b :title="row?.url">{{row ? jobLabel(row) : jobId}}</b>
            <small>{{jobId}}</small>
          </div>
          <button class="ghost small" @click="$emit('close')" aria-label="ปิด">✕</button>
        </header>
        <div class="log-live" v-if="progress"><span class="live-dot" v-if="status === 'running'"></span>{{progress}}</div>
        <div class="log-tools">
          <label class="check"><input type="checkbox" v-model="follow"> ติดตามบรรทัดล่าสุด</label>
          <label class="check"><input type="checkbox" v-model="hideProgress"> ซ่อนบรรทัด progress</label>
          <input v-model="search" class="filter grow" placeholder="กรองบรรทัด…">
          <button class="ghost small" @click="copy">คัดลอก</button>
          <button class="ghost small" @click="download">ดาวน์โหลด</button>
        </div>
        <div class="log-body" ref="body" @scroll="onScroll">
          <button class="ghost small older" v-if="start > 0" @click="loadOlder" :disabled="loadingOlder">{{loadingOlder ? 'กำลังโหลด…' : 'โหลดบรรทัดก่อนหน้า'}}</button>
          <div class="log-note" v-if="hiddenCount">ซ่อน {{hiddenCount}} บรรทัด{{search ? '' : ' progress'}}</div>
          <div v-for="line in visible" :key="line.id" class="ln" :class="'k-' + line.kind">{{line.text}}</div>
          <div class="log-note" v-if="!lines.length && !loading">log ยังว่างอยู่</div>
          <div class="log-note" v-if="loading">กำลังโหลด log…</div>
        </div>
        <footer class="log-foot">
          <span>{{lines.length}} บรรทัด · {{size(bytes)}}</span>
          <button class="small" v-if="!follow" @click="jumpToEnd">ไปบรรทัดล่าสุด ↓</button>
        </footer>
      </aside>`,
  }

  return {
    STATUS, GROUP, FILTERS, KIND, ACTIVE, RETRYABLE, BILI_QUALITIES, BILI_CODECS, BILI_AUDIO, biliOptionHint,
    load, save, size, duration, clock, ago, bitrate, basename, dirname,
    statusLabel, laneOf, kindLabel, jobLabel, showPercent, jobFacts, jobError, sortJobs,
    JobCard, LogDrawer,
  }
})()
