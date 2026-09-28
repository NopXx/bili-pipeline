const { createApp } = Vue
const { GROUP, FILTERS, ACTIVE } = Shared

// --- HLS profile ---------------------------------------------------------------
const HLS_DEFAULTS = {
  mode: 'copy', height: 1080, videoBitrate: '8M', gpuTonemap: true, autoBitrate: true,
  rungs: [{ h: 'raw', on: false, rate: '' }, { h: 2160, on: true, rate: '16M' }, { h: 1440, on: true, rate: '10M' },
    { h: 1080, on: true, rate: '8M' }, { h: 720, on: false, rate: '4M' }, { h: 480, on: false, rate: '2M' }],
  audio: ['2', 'raw'], audioBitrate: 'auto', segment: 6, poster: 5, upload: true, keepLocal: false,
}
const HLS_MODES = [
  ['copy', 'Copy', 'เร็ว คุณภาพเท่าต้นฉบับ'], ['encode', 'Encode', 'H.264 SDR ความสูงเดียว'],
  ['ladder', 'Ladder', 'หลายความละเอียด'], ['hdr_auto', 'HDR Auto', '4K HDR + 1080/720 SDR'],
  ['preserve', 'Preserve HDR', 'HEVC 10-bit'],
]

// The request body /api/process and /api/remote/queue expect.
function hlsProfile(c) {
  const rungs = c.rungs.filter(r => r.on)
  return {
    copy_video: c.mode === 'copy', reencode: c.mode === 'encode', auto_hdr: c.mode === 'hdr_auto',
    preserve_hdr: c.mode === 'preserve', height: c.mode === 'encode' ? +c.height : 0, ladder: c.mode === 'ladder',
    ladder_heights: rungs.map(r => r.h).join(','),
    // 'auto' lets the engine cap each rung at that file's own source bitrate.
    ladder_bitrates: rungs.filter(r => r.h !== 'raw').map(r => `${r.h}:${c.autoBitrate ? 'auto' : r.rate}`).join(','),
    video_bitrate: c.videoBitrate, audio_channels: c.audio.join(','), audio_bitrate: c.audioBitrate,
    segment_seconds: +c.segment, poster_seconds: +c.poster, gpu_tonemap: c.gpuTonemap || c.mode === 'hdr_auto',
    copy_audio: c.audio.includes('raw'), upload: c.upload, keep_local: c.keepLocal,
  }
}

// Probe-aware HLS settings. `probe` is empty when the source is not local yet
// (the Drive library queue): heights then go up to 2160 and HDR modes are off.
const HlsConfig = {
  props: { config: { type: Object, required: true }, probe: { type: Array, default: () => [] } },
  data() { return { HLS_MODES } },
  computed: {
    maxHeight() { return this.probe.length ? Math.min(...this.probe.map(m => this.tierHeight(m) || 99999)) : 2160 },
    hasHdr() { return this.probe.length > 0 && this.probe.every(m => m.video.hdr) },
    hasHevcHdr() { return this.hasHdr && this.probe.every(m => m.video.codec === 'hevc' && +m.video.height >= 2160) },
    availableHeights() { return [2160, 1440, 1080, 720, 480].filter(h => h < this.maxHeight) },
    inputSize() { return this.probe.reduce((n, m) => n + (+m.size || 0), 0) },
    estimate() {
      const c = this.config
      let total = 0
      for (const m of this.probe) {
        let video = 0
        if (c.mode === 'copy') video = this.sourceRate(m)
        else if (c.mode === 'encode' || c.mode === 'preserve') video = this.rate(c.videoBitrate)
        else if (c.mode === 'hdr_auto') video = this.sourceRate(m) + this.rate(this.suggested(1080, m)) + this.rate(this.suggested(720, m))
        else video = c.rungs.filter(r => r.on && r.h !== 'raw' && +r.h <= this.maxHeight).reduce((n, r) => n + this.rate(c.autoBitrate ? this.suggested(r.h, m) : r.rate), 0) +
          (c.rungs.some(r => r.on && r.h === 'raw') ? this.sourceRate(m) : 0)
        const audio = c.audio.reduce((n, a) => n + (a === 'raw' ? m.audio.reduce((s, x) => s + (+x.bit_rate || 640000), 0) : this.audioRate(+a) * m.audio.length), 0)
        total += (video + audio) * (+m.duration || 0) / 8
      }
      return total
    },
    hasDts() { return this.probe.some(m => m.audio.some(a => a.codec === 'dts')) },
    lateAudio() { return this.probe.some(m => m.audio.some(a => a.first_packet > 0.25)) },
  },
  watch: { probe() { this.configureFromProbe() } },
  methods: {
    size: Shared.size,
    // Cinema crops (1920x800) belong to their 16:9 tier (1080p).
    tierHeight(m) {
      const w = +m.video?.width || 0, h = +m.video?.height || 0
      if ((w >= 3800 && h >= 1500) || h >= 2000) return 2160
      if ((w >= 2500 && h >= 1100) || h >= 1350) return 1440
      if ((w >= 1900 && h >= 780) || h >= 1000) return 1080
      if ((w >= 1260 && h >= 500) || h >= 700) return 720
      if ((w >= 840 && h >= 350) || h >= 450) return 480
      return h || 0
    },
    rate(v) { const m = String(v || '').trim().match(/^(\d+(?:\.\d+)?)([mk])?$/i); return m ? +m[1] * (m[2]?.toLowerCase() === 'm' ? 1e6 : m[2] ? 1e3 : 1) : 0 },
    sourceRate(m) { const audio = (m.audio || []).reduce((n, a) => n + (+a.bit_rate || 0), 0); return +m.video?.bit_rate || Math.max(0, (+m.bit_rate || (+m.size || 0) * 8 / (+m.duration || 1)) - audio) },
    audioRate(ch) { return this.config.audioBitrate !== 'auto' ? this.rate(this.config.audioBitrate) : (ch <= 2 ? 192 : 64 * ch) * 1000 },
    suggested(h, m = this.probe[0]) {
      const defaults = { 2160: 16e6, 1440: 10e6, 1080: 8e6, 720: 4e6, 480: 2e6 }
      let value = defaults[h] || 8e6
      // Same as the engine: HEVC/AV1/VP9 bits are worth ~1.6 H.264 bits.
      const factor = ['hevc', 'h265', 'av1', 'vp9'].includes(m?.video?.codec) ? 1.6 : 1
      if (m?.video?.height) value = Math.min(value, this.sourceRate(m) * factor * h / Math.max(1, this.tierHeight(m)))
      return (Math.max(5e5, Math.round(value / 1e5) * 1e5) / 1e6).toFixed(1).replace(/\.0$/, '') + 'M'
    },
    configureFromProbe() {
      if (!this.probe.length) return
      for (const r of this.config.rungs) if (r.h !== 'raw') { r.on = r.on && +r.h <= this.maxHeight; r.rate = this.suggested(+r.h) }
      if (!this.config.rungs.some(r => r.on && r.h !== 'raw')) for (const r of this.config.rungs) if (r.h !== 'raw' && +r.h <= this.maxHeight && +r.h >= 720) r.on = true
      const target = this.availableHeights[0] || 0
      this.config.height = target
      this.config.videoBitrate = this.suggested(target || this.maxHeight)
      if (!this.hasHdr && ['hdr_auto', 'preserve'].includes(this.config.mode)) this.config.mode = 'copy'
    },
    modeDisabled(mode) { return (mode === 'hdr_auto' && !this.hasHevcHdr) || (mode === 'preserve' && !this.hasHdr) },
    setMode(mode) {
      if (this.modeDisabled(mode)) return
      const c = this.config
      c.mode = mode
      if (mode === 'preserve') c.videoBitrate = this.suggested(this.maxHeight)
      else if (mode === 'encode') c.videoBitrate = this.suggested(+c.height || this.maxHeight)
      if (mode === 'hdr_auto') { c.audio = ['2', 'raw']; c.audioBitrate = 'auto'; c.gpuTonemap = true }
    },
  },
  template: `
    <div class="cfg">
      <section class="cfg-section">
        <h3>วิดีโอ</h3>
        <div class="mode-grid">
          <button v-for="m in HLS_MODES" :key="m[0]" class="mode-card" :class="{on: config.mode === m[0]}" :disabled="modeDisabled(m[0])"
            :title="modeDisabled(m[0]) ? (probe.length ? 'ต้นฉบับไม่ใช่ HDR' : 'ต้องตรวจไฟล์ต้นฉบับก่อน') : ''" @click="setMode(m[0])">
            <b>{{m[1]}}</b><small>{{m[2]}}</small>
          </button>
        </div>
        <div class="options" v-if="config.mode === 'encode'">
          <label class="field"><span>ความสูง (ไม่ขยายเกินต้นฉบับ)</span>
            <select v-model.number="config.height" @change="config.videoBitrate = suggested(+config.height || maxHeight)">
              <option :value="0">เท่าต้นฉบับ</option><option v-for="h in availableHeights" :key="h" :value="h">{{h}}p</option>
            </select>
          </label>
          <label class="field"><span>Video bitrate</span><input v-model="config.videoBitrate"></label>
        </div>
        <div class="options" v-if="config.mode === 'preserve'">
          <label class="field"><span>Codec</span><input value="HEVC Main10 · p010le" disabled></label>
          <label class="field"><span>Video bitrate</span><input v-model="config.videoBitrate"></label>
        </div>
        <p class="hint" v-if="config.mode === 'hdr_auto'">เก็บต้นฉบับ 4K HDR และสร้าง 1080p/720p SDR · tonemap ด้วย GPU เมื่อพร้อม ไม่พร้อมจะใช้ CPU</p>
        <p class="hint" v-if="config.mode === 'copy'">ไฟล์ H.264 คัดลอกสตรีมโดยไม่เข้ารหัสใหม่ · codec อื่นจะถูกแปลงเป็น H.264 ที่ความละเอียดเดิม</p>
        <label class="check" v-if="config.mode === 'ladder'">
          <input type="checkbox" v-model="config.autoBitrate"> bitrate อัตโนมัติ: ไม่เกินต้นฉบับ คำนวณแยกทีละไฟล์
        </label>
        <div class="rungs" v-if="config.mode === 'ladder'">
          <label class="rung" v-for="r in config.rungs" :key="r.h" :class="{off: r.h !== 'raw' && +r.h > maxHeight}">
            <input type="checkbox" v-model="r.on" :disabled="r.h !== 'raw' && +r.h > maxHeight">
            <span>{{r.h === 'raw' ? 'ต้นฉบับ (raw)' : r.h + 'p'}}</span>
            <span v-if="r.h !== 'raw' && config.autoBitrate" class="auto-rate">{{probe.length ? '≈ ' + suggested(r.h) : 'auto'}}</span>
            <input v-else-if="r.h !== 'raw'" v-model="r.rate" :disabled="!r.on || +r.h > maxHeight" aria-label="bitrate">
            <small v-else class="muted">คัดลอกสตรีมเดิม</small>
          </label>
        </div>
        <label class="check" v-if="['ladder', 'encode'].includes(config.mode)" style="margin-top:8px">
          <input type="checkbox" v-model="config.gpuTonemap"> GPU tonemap HDR → SDR (ใช้เมื่อต้นฉบับเป็น HDR)
        </label>
      </section>
      <section class="cfg-section">
        <h3>เสียง</h3>
        <div class="option-flags">
          <label class="check" v-for="a in [['2','Stereo AAC'],['6','5.1 AAC'],['raw','ต้นฉบับ (Atmos / FLAC / DTS)']]" :key="a[0]">
            <input type="checkbox" :value="a[0]" v-model="config.audio"> {{a[1]}}
          </label>
        </div>
        <label class="field" style="margin-top:8px"><span>Audio bitrate</span><input v-model="config.audioBitrate" placeholder="auto"></label>
        <p class="hint">auto: Stereo 192k · Surround 64k ต่อ channel · ต้นฉบับคงค่าเดิม</p>
        <p class="hint" v-if="hasDts">เสียง DTS จะถูกแปลงเป็น AAC ตามที่เลือก (Stereo / 5.1) เพื่อให้เบราว์เซอร์เล่นได้ · ติ๊ก “ต้นฉบับ” ถ้าต้องการเก็บ DTS เดิมไว้ด้วย</p>
        <p class="hint warn" v-if="lateAudio">เสียงเริ่มช้ากว่าภาพ: ระบบจะเติมความเงียบและปรับ timeline ให้ตรงกันเอง</p>
      </section>
      <section class="cfg-section">
        <h3>แพ็กเกจ</h3>
        <div class="options">
          <label class="field"><span>ความยาว segment · {{config.segment}} วิ</span><input type="range" min="2" max="15" v-model.number="config.segment"></label>
          <label class="field"><span>ภาพปกที่วินาที</span><input type="number" min="0" v-model.number="config.poster"></label>
        </div>
        <slot></slot>
      </section>
      <div class="estimate" v-if="probe.length">
        <small class="muted">ขนาดเดิม → ขนาดโดยประมาณ</small>
        <strong>{{size(inputSize)}} → {{size(estimate)}}</strong>
        <small class="muted" v-if="inputSize">{{estimate <= inputSize ? 'ลดลง' : 'เพิ่มขึ้น'}} {{Math.abs((estimate / inputSize - 1) * 100).toFixed(1)}}%</small>
      </div>
    </div>`,
}

// --- page ------------------------------------------------------------------------
const BILI_DEFAULTS = { quality: 'auto', codec: 'AVC/H.264', audio_quality: 'auto', container: 'mp4', subtitle: false, redownload: false }
const VIDEO = /\.(mp4|mkv|mov|webm|m4v|avi|ts)$/i
const REMOTE_PROBE_SAMPLE = 3

createApp({
  data() { return {
    token: localStorage.getItem('bwt') || '', tokenDraft: '', editingToken: false,
    online: true, tokenInvalid: false, lastError: '', health: null, healthAt: 0,
    view: Shared.load('studio-view', 'queue'), toasts: [],
    jobs: [], jobFilter: 'all', laneFilter: 'all', jobSearch: '', logJob: '',
    now: Date.now(), clockSkew: 0, timer: null,
    source: 'bili',
    biliUrl: '', episodes: [], selectedEpisodes: [], parsing: false,
    biliOptions: { ...BILI_DEFAULTS, ...Shared.load('studio-bili-options', {}), redownload: false },
    BILI_QUALITIES: Shared.BILI_QUALITIES, BILI_CODECS: Shared.BILI_CODECS, BILI_AUDIO: Shared.BILI_AUDIO,
    torrentSource: '', torrentData: '', torrentFileName: '', torrentName: '', torrentFiles: [], selectedTorrent: [], torrentFilter: '',
    torrentUpload: { upload: false, removeLocal: true, ...Shared.load('studio-torrent-upload', {}) },
    inspectionJob: '', inspectionPending: false,
    driveLink: '',
    remote: { root: '', path: '', items: [], selected: [], sizes: {}, busy: false, error: '', loaded: false, deleteSource: false, probes: {}, probing: false },
    remoteProbeTimer: null, remoteSort: Shared.load('studio-remote-sort', 'name'),
    files: [], selectedFiles: [], fileSearch: '', fileSort: 'modified', filesLoadedAt: 0,
    convertFiles: [], probe: [], probeBusy: false, inspected: [],
    config: { ...structuredClone(HLS_DEFAULTS), ...Shared.load('studio-hls', {}) },
  } },
  computed: {
    visible() { return this.jobs.filter(job => job.kind !== 'torrent_inspect') },
    counts() {
      const counts = { running: 0, waiting: 0, done: 0, failed: 0 }
      for (const job of this.visible) counts[GROUP[job.status] || 'failed']++
      return counts
    },
    lanes() {
      const gpus = this.health?.convert_gpus || []
      return [
        { id: 'download', name: 'ดาวน์โหลด', hint: 'Bilibili · BitTorrent · Drive' },
        { id: 'convert', name: 'แปลง HLS', hint: gpus.length ? `GPU ${gpus.join(', ')} · พร้อมกัน ${gpus.length} งาน` : 'ทีละงาน' },
        { id: 'upload', name: 'อัปโหลด', hint: 'Google Drive' },
      ].map(lane => ({
        ...lane,
        // Running first, then the waiting ones in the order they will start.
        jobs: this.visible.filter(job => ACTIVE.includes(job.status) && Shared.laneOf(job) === lane.id)
          .sort((a, b) => (a.status !== 'running') - (b.status !== 'running') || String(a.created).localeCompare(String(b.created))),
      }))
    },
    filterLabel() { return FILTERS[this.jobFilter] },
    history() {
      const search = this.jobSearch.trim().toLowerCase()
      const groups = this.jobFilter === 'all' ? ['done', 'failed'] : [this.jobFilter]
      return this.visible.filter(job =>
        groups.includes(GROUP[job.status]) &&
        (this.laneFilter === 'all' || Shared.laneOf(job) === this.laneFilter) &&
        (!search || `${Shared.jobLabel(job)} ${job.url} ${job.job}`.toLowerCase().includes(search)))
    },
    logJobRow() { return this.jobs.find(job => job.job === this.logJob) },
    gpuLabel() { const gpus = this.health?.convert_gpus || []; return gpus.length ? `GPU ${gpus.length} ตัว` : 'ไม่มี GPU' },
    biliOptionHint() { return Shared.biliOptionHint(this.biliOptions) },
    biliCodecHint() { return this.biliOptions.codec === 'AVC/H.264' ? '' : 'HEVC/AV1 จะต้องเข้ารหัสใหม่ตอนแปลง HLS · H.264 แปลงได้โดยคัดลอกสตรีม' },
    allTorrentSelected() { return this.torrentFiles.length > 0 && this.selectedTorrent.length === this.torrentFiles.length },
    selectedTorrentSize() { const chosen = new Set(this.selectedTorrent); return this.torrentFiles.reduce((n, f) => n + (chosen.has(f.index) ? f.size : 0), 0) },
    visibleTorrentFiles() { const q = this.torrentFilter.trim().toLowerCase(); return q ? this.torrentFiles.filter(f => f.path.toLowerCase().includes(q)) : this.torrentFiles },
    remoteCrumbs() {
      const parts = this.remote.path ? this.remote.path.split('/') : []
      return [{ name: this.remote.root || 'Drive', path: '' }, ...parts.map((name, i) => ({ name, path: parts.slice(0, i + 1).join('/') }))]
    },
    // Probing reads each file over the network, so a big selection (a whole
    // season) shows a few samples; auto bitrate still measures every file
    // when it converts.
    remoteProbeTargets() { return this.remote.selected.slice(0, REMOTE_PROBE_SAMPLE) },
    remoteProbe() { return this.remoteProbeTargets.map(p => this.remote.probes[p]).filter(m => m && !m.error) },
    remoteProbeErrors() { return this.remoteProbeTargets.map(p => this.remote.probes[p]).filter(m => m?.error) },
    remoteProbePending() { return this.remoteProbeTargets.filter(p => !this.remote.probes[p]).length },
    // Folders first, then files; both in the chosen order (names compare numerically: E2 < E10).
    // Drive folders have no size and are never videos, so those orders keep folders A–Z.
    remoteItems() {
      const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
      const time = f => Date.parse(f.modified) || 0
      const sorters = {
        name: byName,
        'name-desc': (a, b) => byName(b, a),
        size: (a, b) => (b.size || 0) - (a.size || 0) || byName(a, b),
        modified: (a, b) => time(b) - time(a) || byName(a, b),
        video: (a, b) => (b.video - a.video) || byName(a, b),
      }
      const sorter = sorters[this.remoteSort] || byName
      const items = [...this.remote.items]
      return [...items.filter(f => f.dir).sort(sorter), ...items.filter(f => !f.dir).sort(sorter)]
    },
    remoteVideos() { return this.remote.items.filter(f => !f.dir && f.video) },
    remoteSelectedSize() { return this.remote.selected.reduce((n, p) => n + (+this.remote.sizes[p] || 0), 0) },
    visibleFiles() {
      const q = this.fileSearch.trim().toLowerCase()
      const rows = q ? this.files.filter(f => f.relative.toLowerCase().includes(q)) : [...this.files]
      const sorters = { size: (a, b) => b.size - a.size, modified: (a, b) => (b.modified || 0) - (a.modified || 0), name: (a, b) => a.relative.localeCompare(b.relative, undefined, { numeric: true }) }
      return rows.sort(sorters[this.fileSort])
    },
    allFilesSelected() { return this.visibleFiles.length > 0 && this.visibleFiles.every(f => this.selectedFiles.includes(f.path)) },
    selectedFilesSize() { const chosen = new Set(this.selectedFiles); return this.files.reduce((n, f) => n + (chosen.has(f.path) ? f.size : 0), 0) },
    selectedVideos() { return this.selectedFiles.filter(p => VIDEO.test(p)) },
  },
  watch: {
    view(value) {
      Shared.save('studio-view', value)
      if (value === 'files') this.loadFiles()
    },
    source(value) { if (value === 'library' && !this.remote.loaded) this.loadRemote('') },
    torrentSource() { this.resetTorrent() },
    remoteSort(value) { Shared.save('studio-remote-sort', value) },
    'remote.selected'() { clearTimeout(this.remoteProbeTimer); this.remoteProbeTimer = setTimeout(() => this.probeRemote(), 700) },
    biliOptions: { deep: true, handler(value) { Shared.save('studio-bili-options', { ...value, redownload: false }) } },
    torrentUpload: { deep: true, handler(value) { Shared.save('studio-torrent-upload', value) } },
    config: { deep: true, handler(value) { Shared.save('studio-hls', value) } },
  },
  methods: {
    size: Shared.size, clock: Shared.clock, bitrate: Shared.bitrate,
    basename: Shared.basename, dirname: Shared.dirname,
    ago(epoch) { return Shared.ago(epoch, this.now) },
    modifiedLabel(iso) { const t = Date.parse(iso); return Number.isFinite(t) ? Shared.ago(t / 1000, this.now) : '' },
    fps(v) { const p = String(v || '').split('/').map(Number); return p.length === 2 && p[1] ? (p[0] / p[1]).toFixed(3).replace(/0+$/, '').replace(/\.$/, '') : v || '?' },
    sourceBitrate(m) {
      const audio = (m.audio || []).reduce((n, a) => n + (+a.bit_rate || 0), 0)
      const rate = +m.video?.bit_rate || Math.max(0, (+m.bit_rate || (+m.size || 0) * 8 / (+m.duration || 1)) - audio)
      return (+m.video?.bit_rate ? '' : '≈ ') + Shared.bitrate(rate)
    },

    // --- plumbing -----------------------------------------------------------
    async api(path, body = {}) {
      if (!this.token) throw Error('ใส่ Access token แล้วกดบันทึกก่อน')
      let response
      try {
        response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Token': this.token }, body: JSON.stringify(body) })
      } catch {
        this.online = false
        this.lastError = 'ติดต่อเซิร์ฟเวอร์ไม่ได้'
        throw Error('ติดต่อเซิร์ฟเวอร์ไม่ได้ ตรวจว่าเว็บเซิร์ฟเวอร์ยังทำงานอยู่')
      }
      this.online = true
      const data = await response.json().catch(() => ({}))
      this.tokenInvalid = response.status === 401
      if (this.tokenInvalid) { this.editingToken = true; throw Error('Access token ไม่ถูกต้อง') }
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
    dismiss(id) { this.toasts = this.toasts.filter(t => t.id !== id) },
    editToken() { this.tokenDraft = ''; this.editingToken = true },
    saveToken() {
      this.token = this.tokenDraft.trim(); this.tokenDraft = ''; this.editingToken = false
      if (this.token) localStorage.setItem('bwt', this.token); else localStorage.removeItem('bwt')
      this.refresh(true)
    },

    // --- queue --------------------------------------------------------------
    async loadJobs() {
      if (!this.token) return
      const rows = await this.api('/api/jobs')
      if (rows.length && rows[0].now) { this.clockSkew = rows[0].now * 1000 - Date.now(); this.now = Date.now() + this.clockSkew }
      this.jobs = Shared.sortJobs(rows)
    },
    setFilter(value) {
      this.view = 'queue'
      if (value === 'running' || value === 'waiting') { this.jobFilter = 'all'; this.$nextTick(() => document.querySelector('.lanes')?.scrollIntoView({ behavior: 'smooth' })); return }
      this.jobFilter = this.jobFilter === value ? 'all' : value
    },
    async action(path, job, done) {
      try { await this.api(path, { job }); await this.loadJobs(); if (done) this.notify(done, 'ok') } catch (error) { this.fail(error) }
    },
    pause(job) { return this.action('/api/pause', job) },
    resume(job) { return this.action('/api/resume', job) },
    async cancel(job) { if (confirm('ยกเลิกงานนี้หรือไม่?')) await this.action('/api/cancel', job, 'ยกเลิกงานแล้ว') },
    async retry(job) {
      if (!confirm('เพิ่มงานนี้เข้าคิวใหม่หรือไม่?')) return
      try {
        const result = await this.api('/api/retry', { job })
        this.notify('เพิ่มงานเข้าคิวใหม่แล้ว', 'ok')
        await this.loadJobs()
        if (this.logJob === job && result.job) this.logJob = result.job
      } catch (error) { this.fail(error) }
    },
    queued(job, message = 'เพิ่มงานเข้าคิวแล้ว') {
      this.notify(message, 'ok')
      this.view = 'queue'
      this.jobFilter = 'all'
      this.loadJobs().then(() => { if (job && this.jobs.some(item => item.job === job)) this.logJob = job }).catch(() => {})
    },

    // --- add: Bilibili --------------------------------------------------------
    async parseBili() {
      if (!this.biliUrl.trim()) return
      this.parsing = true
      try {
        this.episodes = await this.api('/api/parse', { url: this.biliUrl.trim() })
        this.selectedEpisodes = this.episodes.filter(e => !e.needs_reparse).map(e => e.episode_id)
      } catch (error) { this.fail(error) } finally { this.parsing = false }
    },
    async submitBili() {
      try {
        const result = await this.api('/api/pull', { url: this.biliUrl.trim(), episode_ids: this.selectedEpisodes, ...this.biliOptions })
        this.episodes = []; this.selectedEpisodes = []
        this.queued(result.job)
      } catch (error) { this.fail(error) }
    },

    // --- add: BitTorrent ------------------------------------------------------
    resetTorrent() { this.torrentFiles = []; this.selectedTorrent = []; this.torrentFilter = ''; this.inspectionJob = ''; this.inspectionPending = false },
    clearTorrentFile() { this.torrentData = ''; this.torrentFileName = ''; this.resetTorrent(); if (this.$refs.torrentInput) this.$refs.torrentInput.value = '' },
    async chooseTorrent(event) {
      const file = event.target.files?.[0]
      this.torrentData = ''; this.torrentFileName = ''; this.resetTorrent()
      if (!file) return
      if (file.size > 4 * 1024 * 1024) { this.notify('ไฟล์ .torrent ต้องไม่เกิน 4 MB', 'error'); event.target.value = ''; return }
      const bytes = new Uint8Array(await file.arrayBuffer())
      let binary = ''
      for (let i = 0; i < bytes.length; i += 32768) binary += String.fromCharCode(...bytes.subarray(i, i + 32768))
      this.torrentData = btoa(binary)
      this.torrentFileName = file.name
      if (!this.torrentName) this.torrentName = file.name.replace(/\.torrent$/i, '')
    },
    async inspectTorrent() {
      if (this.inspectionPending || (!this.torrentSource.trim() && !this.torrentData)) return
      try {
        const result = await this.api('/api/torrent/inspect', { source: this.torrentSource.trim(), torrent_data: this.torrentData, name: this.torrentName.trim() || this.torrentFileName })
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
        this.selectedTorrent = this.torrentFiles.map(f => f.index)
        if (!this.torrentName) this.torrentName = result.state.name || ''
      } catch (error) { this.inspectionPending = false; this.fail(error) }
    },
    toggleAllTorrent(checked) { this.selectedTorrent = checked ? this.torrentFiles.map(f => f.index) : [] },
    async submitTorrent() {
      if (!this.inspectionJob) { this.notify('อ่านรายการ torrent ใหม่ก่อน', 'error'); return }
      try {
        const result = await this.api('/api/torrent', { inspection_job: this.inspectionJob, selected_files: this.selectedTorrent, name: this.torrentName.trim() || this.torrentFileName,
          upload_source: this.torrentUpload.upload, remove_local: this.torrentUpload.removeLocal })
        this.torrentSource = ''; this.torrentData = ''; this.torrentFileName = ''; this.torrentName = ''; this.resetTorrent()
        this.queued(result.job)
      } catch (error) { this.fail(error) }
    },

    // --- add: Google Drive ----------------------------------------------------
    async submitDrive() {
      if (!this.driveLink.trim()) return
      try { const result = await this.api('/api/drive/download', { source: this.driveLink.trim() }); this.driveLink = ''; this.queued(result.job) } catch (error) { this.fail(error) }
    },
    async loadRemote(path = '') {
      this.remote.busy = true; this.remote.error = ''
      try {
        const r = await this.api('/api/remote/list', { path })
        this.remote.root = r.remote; this.remote.path = r.path; this.remote.items = r.items
        for (const f of r.items) if (!f.dir) this.remote.sizes[f.path] = f.size
        this.remote.loaded = true
      } catch (error) { this.remote.error = error.message } finally { this.remote.busy = false }
    },
    // Read codec/bitrate/HDR/audio of selected Drive files without downloading them.
    async probeRemote() {
      // One file per request, so the count below moves and a changed selection wins quickly.
      const missing = this.remoteProbeTargets.filter(p => !this.remote.probes[p]).slice(0, 1)
      if (!missing.length || this.remote.probing) return
      this.remote.probing = true
      try {
        for (const m of await this.api('/api/remote/probe', { paths: missing })) this.remote.probes[m.path] = m
      } catch (error) { this.fail(error); return } finally { this.remote.probing = false }
      this.probeRemote()
    },
    // Select every video below this folder (a series stored one folder per
    // episode), skipping the ones whose HLS is already on Drive.
    async selectRemoteTree() {
      this.remote.busy = true
      try {
        const r = await this.api('/api/remote/list', { path: this.remote.path, recursive: true })
        const fresh = r.items.filter(f => !f.converted)
        for (const f of r.items) this.remote.sizes[f.path] = f.size
        this.remote.selected = [...new Set([...this.remote.selected, ...fresh.map(f => f.path)])]
        const skipped = r.items.length - fresh.length
        this.notify(`เลือก ${fresh.length} ไฟล์${skipped ? ` · ข้าม ${skipped} ไฟล์ที่แปลงแล้ว` : ''}`, fresh.length ? 'ok' : 'error')
      } catch (error) { this.fail(error) } finally { this.remote.busy = false }
    },
    async queueRemote() {
      const paths = [...this.remote.selected]
      if (!paths.length) return
      if (this.remote.deleteSource && !confirm(`หลังอัปโหลด HLS สำเร็จ จะลบไฟล์ต้นฉบับ ${paths.length} ไฟล์ออกจาก Drive ต่อหรือไม่?`)) return
      try {
        const r = await this.api('/api/remote/queue', { paths, hls: hlsProfile(this.config), delete_remote_source: this.remote.deleteSource })
        this.remote.selected = []
        this.queued(r.jobs[0], `เพิ่ม ${r.jobs.length} ไฟล์เข้าคิว: ดาวน์โหลด → แปลง HLS → อัปโหลด`)
      } catch (error) { this.fail(error) }
    },

    // --- files --------------------------------------------------------------
    async loadFiles() {
      if (!this.token) return
      try {
        this.files = await this.api('/api/files/downloads')
        this.filesLoadedAt = Date.now()
        const present = new Set(this.files.map(f => f.path))
        this.selectedFiles = this.selectedFiles.filter(p => present.has(p))
      } catch (error) { this.fail(error) }
    },
    toggleAllFiles(checked) {
      const visible = new Set(this.visibleFiles.map(f => f.path))
      const others = this.selectedFiles.filter(p => !visible.has(p))
      this.selectedFiles = checked ? [...others, ...visible] : others
    },
    async inspectSelected() {
      if (!this.selectedFiles.length) return
      this.probeBusy = true
      try { this.inspected = await this.api('/api/probe', { paths: this.selectedFiles }) } catch (error) { this.fail(error) } finally { this.probeBusy = false }
    },
    async uploadSource() {
      if (!confirm(`อัปโหลดไฟล์ต้นฉบับ ${this.selectedFiles.length} ไฟล์ขึ้น Drive โดยไม่แปลง HLS?`)) return
      try { const r = await this.api('/api/upload', { files: this.selectedFiles }); this.selectedFiles = []; this.queued(r.job, 'เพิ่มงานอัปโหลดแล้ว') } catch (error) { this.fail(error) }
    },
    async deleteFiles() {
      const paths = [...this.selectedFiles]
      if (!paths.length || !confirm(`ลบไฟล์ที่เลือก ${paths.length} ไฟล์ (${this.size(this.selectedFilesSize)}) ออกจากเครื่องถาวรหรือไม่?`)) return
      try {
        const r = await this.api('/api/delete/downloads', { paths })
        this.selectedFiles = []; this.inspected = []
        await this.loadFiles()
        this.notify(`ลบแล้ว ${r.deleted} ไฟล์${r.failed ? ` · ลบไม่ได้ ${r.failed} ไฟล์ (อาจมีงานกำลังใช้อยู่)` : ''}`, r.failed ? 'error' : 'ok')
      } catch (error) { this.fail(error) }
    },

    // --- convert ------------------------------------------------------------
    async openConverter(paths) {
      const videos = paths.filter(p => VIDEO.test(p))
      if (!videos.length) { this.notify('เลือกไฟล์วิดีโอก่อน', 'error'); return }
      if (videos.length !== paths.length) this.notify('ข้ามไฟล์ที่ไม่ใช่วิดีโอ', 'info')
      this.convertFiles = videos
      this.view = 'convert'
      await this.probeConvert()
    },
    async probeConvert() {
      if (!this.convertFiles.length) { this.probe = []; return }
      this.probeBusy = true
      try { this.probe = await this.api('/api/probe', { paths: this.convertFiles }) } catch (error) { this.fail(error) } finally { this.probeBusy = false }
    },
    removeConvertFile(path) { this.convertFiles = this.convertFiles.filter(p => p !== path); this.probeConvert() },
    async startConvert() {
      const c = this.config
      if (!this.convertFiles.length || !c.audio.length || (c.mode === 'ladder' && !c.rungs.some(r => r.on))) return
      try {
        const r = await this.api('/api/process', { files: this.convertFiles, ...hlsProfile(c) })
        this.convertFiles = []; this.probe = []
        this.queued(r.job, r.jobs?.length > 1 ? `เพิ่ม ${r.jobs.length} ไฟล์เข้าคิวแปลง (แยกงานละไฟล์ ใช้ทุก GPU)` : 'เริ่มแปลง HLS แล้ว')
      } catch (error) { this.fail(error) }
    },

    // --- refresh loop -------------------------------------------------------
    async refresh(force = false) {
      this.now = Date.now() + this.clockSkew
      if (!this.token) return
      try {
        await this.loadJobs()
        if (force || this.now - this.healthAt > 30000) { this.healthAt = this.now; this.health = await this.api('/api/health') }
      } catch (error) { this.lastError = error.message; if (force) this.fail(error) }
      if (this.view === 'files' && Date.now() - this.filesLoadedAt > 10000) this.loadFiles()
      if (this.inspectionPending) this.checkInspection()
    },
  },
  mounted() {
    this.refresh(true)
    if (this.view === 'files') this.loadFiles()
    if (this.view === 'convert' && !this.convertFiles.length) this.view = 'files'
    this.timer = setInterval(() => this.refresh(), 2000)
  },
  beforeUnmount() { clearInterval(this.timer) },
})
  .component('bili-account', BiliAccount)
  .component('job-card', Shared.JobCard)
  .component('log-drawer', Shared.LogDrawer)
  .component('hls-config', HlsConfig)
  .mount('#studio')
