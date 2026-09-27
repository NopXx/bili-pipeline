const { createApp } = Vue

createApp({
  data() { return {
    token: localStorage.getItem('bwt') || '', tab: 'download', source: 'drive', message: '',
    driveLink: '', torrentSource: '', torrentData: '', torrentFiles: [], selectedTorrent: [], inspectionJob: '', inspectionPending: false,
    biliUrl: '', episodes: [], selectedEpisodes: [], files: [], selectedFiles: [], jobs: [],
    logJob: '', logText: '', logRequest: 0, timer: null,
  } },
  watch: {
    tab(value) { if (value === 'files') this.loadFiles(); if (value === 'queue') this.loadJobs() },
    torrentSource() { this.torrentFiles = []; this.selectedTorrent = []; this.inspectionJob = ''; this.inspectionPending = false },
  },
  methods: {
    async api(path, body = {}) {
      if (!this.token) throw Error('กรอก Access token แล้วกด Save ก่อน')
      const response = await fetch(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Token': this.token },
        body: JSON.stringify(body),
      })
      const data = await response.json()
      if (!response.ok || data.error) throw Error(data.error || `HTTP ${response.status}`)
      return data
    },
    saveToken() { this.token = this.token.trim(); if (this.token) localStorage.setItem('bwt', this.token); else localStorage.removeItem('bwt'); this.refresh() },
    size(bytes) { let n = +bytes || 0; const units = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ } return `${n.toFixed(i ? 1 : 0)} ${units[i]}` },
    jobLabel(job) { return String(job.url || job.job).split('/').pop() || job.job },
    async submitDrive() { try { const result = await this.api('/api/drive/download', { source: this.driveLink.trim() }); this.message = `เพิ่มงาน ${result.job} แล้ว`; this.tab = 'queue'; this.loadJobs() } catch (error) { this.message = error.message } },
    async chooseTorrent(event) {
      const file = event.target.files?.[0]
      this.torrentData = ''
      this.torrentFiles = []
      this.inspectionJob = ''
      this.inspectionPending = false
      if (!file) return
      if (file.size > 4 * 1024 * 1024) { this.message = 'ไฟล์ .torrent ต้องไม่เกิน 4 MB'; event.target.value = ''; return }
      const bytes = new Uint8Array(await file.arrayBuffer())
      let binary = ''
      for (let i = 0; i < bytes.length; i += 32768) binary += String.fromCharCode(...bytes.subarray(i, i + 32768))
      this.torrentData = btoa(binary)
    },
    async inspectTorrent() {
      try {
        const result = await this.api('/api/torrent/inspect', { source: this.torrentSource.trim(), torrent_data: this.torrentData })
        this.inspectionJob = result.job
        this.inspectionPending = true
        this.message = 'กำลังอ่านรายการไฟล์ torrent…'
      } catch (error) { this.message = error.message }
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
        this.message = `เลือกไฟล์ที่ต้องการดาวน์โหลด (${this.torrentFiles.length} รายการ)`
      } catch (error) { this.message = error.message }
    },
    toggleAllTorrent(checked) { this.selectedTorrent = checked ? this.torrentFiles.map(file => file.index) : [] },
    async submitTorrent() {
      if (!this.inspectionJob) { this.message = 'อ่านรายการ torrent ใหม่ก่อน'; return }
      try {
        const result = await this.api('/api/torrent', { inspection_job: this.inspectionJob, selected_files: this.selectedTorrent })
        this.message = `เพิ่มงาน ${result.job} แล้ว`; this.tab = 'queue'; this.loadJobs()
      } catch (error) { this.message = error.message }
    },
    async parseBili() {
      try {
        this.episodes = await this.api('/api/parse', { url: this.biliUrl.trim() })
        this.selectedEpisodes = this.episodes.filter(item => !item.needs_reparse).map(item => item.episode_id)
      } catch (error) { this.message = error.message }
    },
    async submitBili() {
      try {
        const result = await this.api('/api/pull', { url: this.biliUrl.trim(), episode_ids: this.selectedEpisodes })
        this.message = `เพิ่มงาน ${result.job} แล้ว`; this.tab = 'queue'; this.loadJobs()
      } catch (error) { this.message = error.message }
    },
    async loadFiles() { if (!this.token) return; try { this.files = await this.api('/api/files/downloads') } catch (error) { this.message = error.message } },
    async upload() {
      try {
        const result = await this.api('/api/upload', { files: this.selectedFiles })
        this.message = `เพิ่มงานอัปโหลด ${result.job} แล้ว`; this.tab = 'queue'; this.loadJobs()
      } catch (error) { this.message = error.message }
    },
    async loadJobs() { if (!this.token) return; try { this.jobs = (await this.api('/api/jobs')).filter(job => job.lane !== 'convert' && job.kind !== 'torrent_inspect').sort((a, b) => b.created.localeCompare(a.created) || b.job.localeCompare(a.job)) } catch (error) { this.message = error.message } },
    async showLog(job) {
      if (this.logJob !== job) { this.logJob = job; this.logText = 'กำลังโหลด log…' }
      const request = ++this.logRequest
      try {
        const result = await this.api('/api/status', { job })
        if (this.logJob === job && this.logRequest === request) this.logText = result.log || ''
      } catch (error) {
        if (this.logJob === job && this.logRequest === request) this.message = error.message
      }
    },
    async action(path, job) { try { await this.api(path, { job }); await this.loadJobs(); if (this.logJob === job) await this.showLog(job) } catch (error) { this.message = error.message } },
    pause(job) { return this.action('/api/pause', job) },
    resume(job) { return this.action('/api/resume', job) },
    async cancel(job) { if (confirm('ยกเลิกงานนี้หรือไม่?')) await this.action('/api/cancel', job) },
    async retry(job) { if (!confirm('เพิ่มงานนี้เข้าคิวใหม่หรือไม่?')) return; try { await this.api('/api/retry', { job }); await this.loadJobs() } catch (error) { this.message = error.message } },
    async refresh() { await this.loadJobs(); if (this.tab === 'files') await this.loadFiles(); if (this.inspectionPending) await this.checkInspection(); if (this.logJob) await this.showLog(this.logJob) },
  },
  mounted() { this.refresh(); this.timer = setInterval(() => this.refresh(), 2000) },
  beforeUnmount() { clearInterval(this.timer) },
}).mount('#transfer-app')
