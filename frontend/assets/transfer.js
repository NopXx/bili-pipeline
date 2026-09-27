const { createApp } = Vue
const { GROUP, FILTERS } = Shared

const BILI_DEFAULTS = { quality: 'auto', codec: 'auto', audio_quality: 'auto', container: 'mp4', subtitle: false, redownload: false }

createApp({
  data() { return {
    token: localStorage.getItem('bwt') || '', tokenDraft: '', editingToken: false,
    online: true, tokenInvalid: false, lastError: '', health: null,
    view: 'queue', source: 'torrent', toasts: [],
    driveLink: '', torrentSource: '', torrentData: '', torrentFileName: '', torrentFiles: [], selectedTorrent: [], torrentFilter: '',
    inspectionJob: '', inspectionPending: false,
    biliUrl: '', episodes: [], selectedEpisodes: [], parsing: false,
    biliOptions: { ...BILI_DEFAULTS, ...Shared.load('bili-options', {}), redownload: false },
    BILI_QUALITIES: Shared.BILI_QUALITIES, BILI_CODECS: Shared.BILI_CODECS, BILI_AUDIO: Shared.BILI_AUDIO,
    files: [], selectedFiles: [], fileSearch: '', fileSort: 'size', filesLoadedAt: 0,
    jobs: [], jobFilter: 'all', laneFilter: 'all', jobSearch: '', logJob: '',
    now: Date.now(), clockSkew: 0, healthAt: 0, timer: null,
  } },
  computed: {
    transferJobs() { return this.jobs.filter(job => Shared.laneOf(job) !== 'convert' && job.kind !== 'torrent_inspect') },
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
        (this.laneFilter === 'all' || Shared.laneOf(job) === this.laneFilter) &&
        (!search || `${Shared.jobLabel(job)} ${job.url} ${job.job}`.toLowerCase().includes(search)))
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
    biliOptionHint() { return Shared.biliOptionHint(this.biliOptions) },
  },
  watch: {
    view(value) { if (value === 'files') this.loadFiles() },
    torrentSource() { this.resetTorrent() },
    biliOptions: { deep: true, handler(value) { Shared.save('bili-options', { ...value, redownload: false }) } },
  },
  methods: {
    size: Shared.size, clock: Shared.clock, basename: Shared.basename, dirname: Shared.dirname,
    ago(epoch) { return Shared.ago(epoch, this.now) },

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
      this.loadJobs().then(() => { if (this.jobs.some(item => item.job === job)) this.logJob = job })
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
        this.jobs = Shared.sortJobs(rows)
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
      try {
        const result = await this.api('/api/retry', { job })
        this.notify('เพิ่มงานเข้าคิวใหม่แล้ว', 'ok')
        await this.loadJobs()
        if (this.logJob === job && result.job) this.logJob = result.job
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
    },
  },
  mounted() {
    this.refresh(true)
    this.timer = setInterval(() => this.refresh(), 2000)
  },
  beforeUnmount() { clearInterval(this.timer) },
})
  .component('bili-account', BiliAccount)
  .component('job-card', Shared.JobCard)
  .component('log-drawer', Shared.LogDrawer)
  .mount('#transfer-app')
