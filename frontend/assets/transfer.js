const { createApp } = Vue
const { GROUP, FILTERS } = Shared

const UNSHACKLE_DEFAULTS = { service: '', proxy: '' }
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
    // options persist per browser; title and episodes are per job.
    // Same flow as unshackle-ui: search/open -> episodes -> tracks -> pick -> queue.
    un: { enabled: null, services: [], searchable: [], input: '', busy: '', results: [], titleId: '',
      titles: [], selected: [], tracks: null, video: '', audio: [], subs: [], filter: { height: '', range: '', codec: '' },
      audioQuick: 'rec', subQuick: 'rec', audioSearch: '', subSearch: '',
      options: { ...UNSHACKLE_DEFAULTS, ...Shared.load('unshackle-options', {}) } },
    unLoadSeq: 0,
    BILI_QUALITIES: Shared.BILI_QUALITIES, BILI_CODECS: Shared.BILI_CODECS, BILI_AUDIO: Shared.BILI_AUDIO,
    files: [], selectedFiles: [], fileSearch: '', fileSort: 'size', filesLoadedAt: 0, uploads: [],
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
    unCanSearch() { return this.un.searchable.includes(this.un.options.service) },
    // A URL, or one token with a digit/dash (entity-…, B0…, uuid), is opened; anything else is searched.
    unLooksLikeId() {
      const value = this.un.input.trim()
      return !this.unCanSearch || value.includes('://') || (!/\s/.test(value) && /[0-9-]/.test(value) && value.length >= 8)
    },
    unEpisodic() { return this.un.titles.some(t => t.season != null && t.number != null) },
    unAllVideos() {
      return [...(this.un.tracks?.video || [])].sort((a, b) => (b.height - a.height) || ((b.bitrate || 0) - (a.bitrate || 0)))
    },
    unVideos() {
      const f = this.un.filter
      return this.unAllVideos.filter(v => (!f.height || v.height === Number(f.height)) && (!f.range || v.range === f.range) &&
        (!f.codec || (v.codec_display || v.codec) === f.codec))
    },
    unHeights() { return [...new Set(this.unAllVideos.map(v => v.height))] },
    unRanges() { return [...new Set(this.unAllVideos.map(v => v.range))] },
    unCodecs() { return [...new Set(this.unAllVideos.map(v => v.codec_display || v.codec))] },
    unTitleLabel() {
      const first = this.un.titles[0] || {}
      return this.unEpisodic ? `${first.series_title || first.name} · tracks ของ ${this.un.selected[0] || ''}` : `${first.name || ''}${first.year ? ` (${first.year})` : ''}`
    },
    unPickLabel() {
      const v = this.unAllVideos.find(item => item.id === this.un.video)
      return v ? `${v.width}×${v.height} ${v.range} · เสียง ${this.un.audio.join(', ') || '-'} · ซับ ${this.un.subs.join(', ') || 'ไม่เอา'}` : 'เลือกวิดีโอ'
    },
    unAudioLangs() {
      const langs = new Map()
      for (const a of this.un.tracks?.audio || []) {
        const lang = langs.get(a.language) || { code: a.language, orig: false, kinds: new Set() }
        lang.orig ||= a.is_original || a.is_original_lang
        // unshackle reports channel count (6, 8); show the layout people know (5.1, 7.1).
        const layout = { 1: '1.0', 2: '2.0', 6: '5.1', 8: '7.1' }[Math.round(a.channels)] || a.channels || ''
        lang.kinds.add(`${a.codec_display || a.codec} ${layout}${a.atmos ? ' Atmos' : ''}`.trim())
        langs.set(a.language, lang)
      }
      return [...langs.values()].filter(l => l.code).map(l => ({ ...l, detail: [...l.kinds].slice(0, 3).join(' · ') }))
        .sort((a, b) => (b.orig - a.orig) || a.code.localeCompare(b.code))
    },
    unSubLangs() {
      const langs = new Map()
      for (const t of this.un.tracks?.subtitles || []) {
        if (!t.language) continue
        const lang = langs.get(t.language) || { code: t.language, forced: false, sdh: false, kinds: new Set() }
        lang.forced ||= !!t.forced
        lang.sdh ||= !!(t.sdh || t.cc)
        lang.kinds.add(t.forced ? 'Forced' : (t.sdh || t.cc) ? 'SDH' : 'ปกติ')
        langs.set(t.language, lang)
      }
      return [...langs.values()].map(l => ({ ...l, detail: [...l.kinds].join(' · ') })).sort((a, b) => a.code.localeCompare(b.code))
    },
    // Same filters as unshackle-ui: "แนะนำ" = original + English + Thai (+ whatever is already picked).
    unRecommended() {
      const orig = this.unAudioLangs.filter(l => l.orig).map(l => l.code.split('-')[0])
      return code => ['en', 'th', ...orig].includes(code.split('-')[0])
    },
    unAudioRows() {
      const q = this.un.audioSearch.trim().toLowerCase(), base = c => c.split('-')[0]
      return this.unAudioLangs.filter(l =>
        (this.un.audioQuick !== 'rec' || l.orig || this.un.audio.includes(l.code) || this.unRecommended(l.code)) &&
        (this.un.audioQuick !== 'th' || base(l.code) === 'th') && (this.un.audioQuick !== 'en' || base(l.code) === 'en') &&
        (this.un.audioQuick !== 'atmos' || l.detail.includes('Atmos')) && (!q || `${l.code} ${l.detail}`.toLowerCase().includes(q)))
    },
    unSubRows() {
      const q = this.un.subSearch.trim().toLowerCase(), base = c => c.split('-')[0]
      return this.unSubLangs.filter(l =>
        (this.un.subQuick !== 'rec' || this.un.subs.includes(l.code) || this.unRecommended(l.code)) &&
        (this.un.subQuick !== 'th' || base(l.code) === 'th') && (this.un.subQuick !== 'en' || base(l.code) === 'en') &&
        (this.un.subQuick !== 'forced' || l.forced) && (this.un.subQuick !== 'sdh' || l.sdh) && (!q || l.code.toLowerCase().includes(q)))
    },
  },
  watch: {
    view(value) { if (value === 'files') this.loadFiles() },
    torrentSource() { this.resetTorrent() },
    biliOptions: { deep: true, handler(value) { Shared.save('bili-options', { ...value, redownload: false }) } },
    'un.options': { deep: true, handler(value) { Shared.save('unshackle-options', value) } },
    source(value) { if (value === 'unshackle' && this.un.enabled === null) this.loadUnshackleServices() },
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
    async loadUnshackleServices() {
      try {
        const result = await this.api('/api/unshackle/services')
        Object.assign(this.un, { enabled: result.enabled, services: result.services, searchable: result.searchable || [] })
        if (result.services.length && !result.services.includes(this.un.options.service)) this.un.options.service = result.services[0]
      } catch (error) { this.fail(error) }
    },
    unCode(t) { return t.season == null || t.number == null ? '' : `S${String(t.season).padStart(2, '0')}E${String(t.number).padStart(2, '0')}` },
    unReset() { Object.assign(this.un, { results: [], titleId: '', titles: [], selected: [], tracks: null, video: '', audio: [], subs: [] }) },
    async unCall(busy, route, body) {
      this.un.busy = busy
      try { return await this.api(route, { service: this.un.options.service, proxy: this.un.options.proxy, ...body }) }
      finally { this.un.busy = '' }
    },
    unGo() { return this.unLooksLikeId ? this.unOpen(this.un.input.trim()) : this.unSearch() },
    async unSearch() {
      this.unReset()
      try { this.un.results = (await this.unCall('search', '/api/unshackle/search', { query: this.un.input.trim() })).results || [] }
      catch (error) { this.fail(error) }
    },
    async unOpen(title) {
      const results = this.un.results
      this.unReset()
      try {
        const titles = (await this.unCall('open', '/api/unshackle/titles', { title })).titles || []
        Object.assign(this.un, { titleId: title, titles, selected: titles.map(this.unCode).filter(Boolean) })
      } catch (error) { this.un.results = results; this.fail(error) }
    },
    async unLoadTracks() {
      const wanted = this.unEpisodic ? this.un.selected[0] : ''
      const body = { title: this.un.titleId, wanted }
      Object.assign(this.un, { tracks: null, video: '', audio: [], subs: [], filter: { height: '', range: '', codec: '' } })
      this.view = 'tracks'
      // A newer load (another episode/title) must win over a slow one still in flight. Compare a
      // counter, not objects: Vue hands back a reactive proxy, never the object that was assigned.
      const load = ++this.unLoadSeq
      try {
        const tracks = await this.unCall('tracks', '/api/unshackle/tracks', body)
        if (load !== this.unLoadSeq) return
        tracks.video = tracks.video || []
        this.un.tracks = tracks
        this.unPickDefaults()
        // Like unshackle-ui there is no range to choose. Most services list every range at once; AMZN
        // fetches one manifest per range, so when only SDR came back, fetch HDR10 and DV too and merge.
        if (tracks.video.length && tracks.video.every(v => v.range === 'SDR')) {
          this.un.busy = 'more'
          const extra = await Promise.allSettled(['HDR10', 'DV'].map(range =>
            this.api('/api/unshackle/tracks', { service: this.un.options.service, proxy: this.un.options.proxy, ...body, range: [range] })))
          if (load !== this.unLoadSeq) return
          this.un.busy = ''
          const video = this.un.tracks.video  // the reactive array, so the table updates
          const seen = new Set(video.map(v => v.id))
          for (const result of extra) {
            for (const v of result.status === 'fulfilled' ? result.value.video || [] : []) {
              if (!seen.has(v.id)) { seen.add(v.id); video.push(v) }
            }
          }
          this.unPickDefaults()
        }
      } catch (error) { this.fail(error) }
    },
    unPickDefaults() {
      // Best rung by default (highest resolution, then bitrate), original-language audio, no subtitles.
      this.un.video = this.unAllVideos[0]?.id || ''
      if (!this.un.audio.length) {
        const orig = this.unAudioLangs.filter(l => l.orig).map(l => l.code)
        this.un.audio = orig.length ? orig : this.unAudioLangs.filter(l => l.code.startsWith('en')).slice(0, 1).map(l => l.code)
      }
    },
    async unSubmit() {
      const v = this.unAllVideos.find(item => item.id === this.un.video)
      if (!v) return
      try {
        const job = (await this.unCall('submit', '/api/unshackle/download', {
          title: this.un.titleId, wanted: this.unEpisodic ? this.un.selected : [],
          quality: String(v.height), vbitrate: String(v.bitrate || ''), range: [v.range],
          a_lang: this.un.audio, s_lang: this.un.subs,
        })).job
        this.queued(job)
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
    chooseLocalFiles() { this.$refs.localInput.click() },
    async uploadLocalFiles(event) {
      const files = [...(event.target.files || [])]
      event.target.value = ''
      if (!files.length) return
      for (const file of files) {
        const row = { id: `${Date.now()}-${Math.random().toString(36).slice(2)}`, name: file.name, loaded: 0, total: file.size || 1, status: 'รอ…' }
        this.uploads.push(row)
        try {
          await this.uploadOneFile(file, row)
          row.loaded = row.total
          row.status = 'เสร็จ'
        } catch (error) { row.status = `ล้มเหลว: ${error.message}` }
      }
      await this.loadFiles()
      setTimeout(() => { this.uploads = this.uploads.filter(u => u.status !== 'เสร็จ') }, 8000)
    },
    uploadOneFile(file, row) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest()
        xhr.open('POST', '/api/files/upload')
        xhr.setRequestHeader('X-Token', this.token)
        xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name))
        xhr.upload.onprogress = e => { if (e.lengthComputable) { row.loaded = e.loaded; row.total = e.total } }
        xhr.onload = () => {
          if (xhr.status === 200) return resolve()
          let message = `HTTP ${xhr.status}`
          try { message = JSON.parse(xhr.responseText).error || message } catch {}
          reject(new Error(message))
        }
        xhr.onerror = () => reject(new Error('ติดต่อเซิร์ฟเวอร์ไม่ได้'))
        xhr.send(file)
      })
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
