// Unshackle tab: a 1:1 port of unshackle-ui's Browse page (src/routes/index.tsx) and title page
// (src/routes/title.$service.$id.tsx), as a Vue mixin for transfer.js. Browse lives in the add panel,
// the title page in the right pane. All calls go through this server's /api/unshackle/* routes.
(function () {
  // unshackle-ui src/lib/download.ts: advanced /api/download options, grouped.
  const ADVANCED_GROUPS = [
    ['Track scope', [
      ['video_only', 'bool', 'Video only'], ['audio_only', 'bool', 'Audio only'], ['subs_only', 'bool', 'Subtitles only'],
      ['chapters_only', 'bool', 'Chapters only'], ['no_video', 'bool', 'No video'], ['no_audio', 'bool', 'No audio'],
      ['no_subs', 'bool', 'No subtitles'], ['no_chapters', 'bool', 'No chapters'],
    ]],
    ['Quality selection', [
      ['best_available', 'bool', 'Best available', 'Continue with best available if requested quality is unavailable'],
      ['worst', 'bool', 'Worst', 'Lowest bitrate within the quality (needs quality)'],
      ['repack', 'bool', 'REPACK tag'], ['latest_episode', 'bool', 'Latest episode only'],
      ['latest_episodes', 'int', 'Latest N episodes', 'Only the N newest episodes; overrides Latest episode only'],
    ]],
    ['Video', [['vbitrate', 'int', 'Video bitrate (kbps)']]],
    ['Audio', [
      ['acodec', 'csv', 'Audio codec', '', 'AAC, EC3, AC3…'], ['abitrate', 'int', 'Audio bitrate (kbps)'],
      ['channels', 'num', 'Channels', '', '2.0, 5.1, 7.1'], ['no_atmos', 'bool', 'Exclude Atmos'],
      ['audio_description', 'bool', 'Audio description'], ['split_audio', 'bool', 'Split audio per codec'],
    ]],
    ['Subtitles & languages', [
      ['forced_subs', 'bool', 'Forced subtitles'], ['exact_lang', 'bool', 'Exact language match'],
      ['require_subs', 'csv', 'Require subtitle langs', '', 'en, es…'], ['sub_format', 'text', 'Subtitle format', '', 'SRT, VTT…'],
      ['lang', 'csv', 'Video+audio lang', '', 'orig, en…'], ['v_lang', 'csv', 'Video lang'],
      ['forced_s_lang', 'csv', 'Forced subtitle langs', 'Implies forced subtitles; a - prefix excludes'],
      ['require_audio', 'csv', 'Require audio langs', 'Fail the job if any is missing'],
      ['require_video', 'csv', 'Require video langs', 'Fail the job if any is missing'],
    ]],
    ['Output', [
      ['profile', 'text', 'Profile'], ['tag', 'text', 'Group tag'],
      ['output_dir', 'text', 'Output directory', "Relative to the server's downloads directory"],
      ['no_folder', 'bool', 'No TV folder'], ['no_source', 'bool', 'No source tag'], ['no_mux', 'bool', 'No mux'],
      ['no_attachments', 'bool', 'No attachments', 'Skip cover art and subtitle fonts'],
    ]],
    ['Pipeline', [
      ['workers', 'int', 'Workers per track'], ['downloads', 'int', 'Concurrent tracks'],
      ['adaptive_workers', 'bool', 'Adaptive workers', 'Scale segment workers to CDN throughput, up to the workers cap'],
      ['download_processes', 'int', 'Download processes', "Split a track's segments across processes (large batches only)"],
      ['continue_downloads', 'bool', 'Resume downloads', 'Keep finished segments and resume a failed download'],
      ['slow', 'text', 'Slow delay', '', 'true or 20-40'], ['skip_dl', 'bool', 'Keys only (skip download)'],
      ['export', 'bool', 'Export manifest JSON'], ['cdm_only', 'bool', 'CDM only for keys'],
      ['cdm', 'text', 'CDM device', 'Server CDM to license with; needs serve.cdm_overrides'], ['enrich', 'bool', 'Enrich title/year'],
    ]],
    ['Tagging IDs', [
      ['tmdb_id', 'int', 'TMDB ID'], ['imdb_id', 'text', 'IMDB ID', '', 'tt1375666'],
      ['anilist_id', 'text', 'AniList ID', '', '21 or mal:21'],
      ['daily', 'bool', 'Daily episodes', 'Fill missing air dates from TVDB (needs enrich)'],
    ]],
    ['Proxy', [
      ['proxy', 'text', 'Proxy', '', 'URI or country code'], ['no_proxy', 'bool', 'Disable proxy'],
      ['no_proxy_download', 'bool', 'Bypass proxy for segments'],
      ['proxy_download', 'text', 'Download proxy', 'Segments only; manifest, license and auth use Proxy'],
    ]],
    ['Cache', [['no_cache', 'bool', 'Bypass title cache'], ['reset_cache', 'bool', 'Reset title cache']]],
  ]
  const ADVANCED_FIELDS = ADVANCED_GROUPS.flatMap(([group, fields]) =>
    fields.map(([key, type, label, help, placeholder]) => ({ key, type, label, help, placeholder, group })))

  // unshackle-ui src/lib/options.ts
  const boolDefault = f => f.default === true
  const strDefault = f => (f.default == null || f.default === false ? '' : String(f.default))
  function blankValues(fields) {
    const v = {}
    for (const f of fields) v[f.key] = f.type === 'bool' ? boolDefault(f) : strDefault(f)
    return v
  }
  /** Form values to a partial request body, dropping anything still at its default. */
  function coerce(fields, values) {
    const out = {}
    for (const f of fields) {
      const raw = values[f.key]
      if (f.type === 'bool') { if ((raw === true) !== boolDefault(f)) out[f.key] = raw === true; continue }
      const s = String(raw ?? '').trim()
      if (s === '' || s === strDefault(f)) continue
      if (f.type === 'int' || f.type === 'num') { if (!Number.isNaN(Number(s))) out[f.key] = Number(s) }
      else if (f.type === 'csv') { const arr = s.split(',').map(x => x.trim()).filter(Boolean); if (arr.length) out[f.key] = arr }
      else if (f.key === 'slow' && (s === 'true' || s === 'false')) out[f.key] = s === 'true'
      else out[f.key] = s
    }
    return out
  }
  function serviceFields(params) {
    return (params || []).filter(p => p.kind === 'option').map(p => ({
      key: p.name,
      type: p.is_flag ? 'bool' : p.multiple ? 'csv' : p.choices?.length ? 'choice' : p.type === 'integer' ? 'int' : p.type === 'float' ? 'num' : 'text',
      label: (p.opts || []).find(o => o.startsWith('--'))?.slice(2) || p.name,
      help: p.help, choices: p.choices, placeholder: p.multiple ? 'a, b, c…' : undefined,
      default: p.is_flag ? p.default === true : p.default == null ? '' : Array.isArray(p.default) ? p.default.join(', ') : String(p.default),
    }))
  }
  // Default subtitles to SubRip (SRT); empty would mux them as WebVTT. Clearable per download.
  const blankAdvanced = () => ({ ...blankValues(ADVANCED_FIELDS), sub_format: 'SRT' })

  // unshackle-ui src/lib/tracks.ts
  const pad2 = n => String(n).padStart(2, '0')
  const wantedCode = t => (t.season == null || t.number == null ? null : `S${pad2(t.season)}E${pad2(t.number)}${t.part == null ? '' : `.${t.part}`}`)
  const isOriginal = t => !!(t.is_original || t.is_original_lang)
  const distinct = xs => [...new Set(xs)]
  const kbps = b => (b ? `${Math.round(b)} kb/s` : null)
  const describeVideo = t => [t.resolution || (t.width && t.height ? `${t.width}x${t.height}` : ''), t.codec_display || t.codec, t.range,
    kbps(t.bitrate), t.fps ? `${Number(t.fps).toFixed(3).replace(/\.?0+$/, '')} fps` : null, t.language].filter(Boolean).join(' · ')
  const describeAudio = t => [t.codec_display || t.codec, t.channels != null ? Number(t.channels).toFixed(1) : null, t.atmos ? 'Atmos' : null,
    kbps(t.bitrate), t.language ? `${t.language}${isOriginal(t) ? ' (original)' : ''}` : null, t.descriptive ? 'AD' : null].filter(Boolean).join(' · ')
  const describeSubtitle = t => [t.codec, t.language, t.forced ? 'Forced' : null, t.sdh ? 'SDH' : null, t.cc ? 'CC' : null].filter(Boolean).join(' · ')
  function summarize(tracks) {
    const a = tracks.audio || [], s = tracks.subtitles || []
    return {
      audioLangs: distinct(a.map(t => t.language || '').filter(Boolean)).sort(),
      originalAudioLangs: distinct(a.filter(isOriginal).map(t => t.language || '').filter(Boolean)),
      subLangs: distinct(s.map(t => t.language || '').filter(Boolean)).sort(),
      counts: { video: (tracks.video || []).length, audio: a.length, subtitles: s.length },
    }
  }

  const QUICK_SERVICES = ['DSNP', 'AMZN', 'NF', 'NF4K', 'CR', 'HMAX']
  const videoKey = t => `${t.width}x${t.height}-${t.range}-${t.codec}-${t.bitrate}`
  const audioKey = t => `${t.codec_display || t.codec}-${t.channels}-${t.atmos}-${t.bitrate}`
  const rangeGroup = t => (t.range === 'DV' ? 'DV' : t.range === 'SDR' ? 'SDR' : 'HDR')
  const base = l => (l || '').split('-')[0]
  const saved = Shared.load('unshackle-browse', {})

  function blankTitle() {
    return {
      service: '', id: '', titles: [], error: '', opening: true,
      epSel: [], loadedSel: [], loading: false, trackError: '', results: [], more: false,
      qualitySel: [], rangeSel: [], audioSel: [], subSel: [], showAllLangs: false, videoPick: null, audioPick: null,
      vRes: 'all', vRange: 'all', vCodec: 'all', vSort: 'default', audioQuick: 'all', subQuick: 'all', audioSearch: '', subSearch: '',
      advanced: blankAdvanced(), svcValues: {}, starting: false, startError: '',
    }
  }

  window.UnshackleMixin = {
    data() { return {
      un: {
        enabled: null, services: [], searchable: [], params: {}, profiles: {},
        // Browse page state; service and proxy persist like unshackle-ui's Settings proxy.
        service: saved.service || '', query: '', profile: '', proxy: saved.proxy || '', noProxy: false,
        searching: false, searchError: '', results: null,
      },
      ut: null,  // the open title page
      utSeq: 0,
    } },
    computed: {
      unQuick() { return QUICK_SERVICES.filter(t => this.un.services.includes(t)) },
      unCanSearch() { return this.un.searchable.includes(this.un.service) },
      unProfiles() { return this.un.profiles[this.un.service] || [] },
      unCanSubmit() { return !!(this.un.query.trim() && this.un.service) },

      utEpisodic() { return !!this.ut && this.ut.titles.some(t => t.season != null && t.number != null) },
      utSplit() { return !!this.ut && this.ut.titles.some(t => t.part != null) },
      utHeader() { const f = this.ut?.titles[0]; return f?.series_title || f?.name || 'Title' },
      utStale() { return !!this.ut && JSON.stringify(this.ut.epSel) !== JSON.stringify(this.ut.loadedSel) },
      utSvcFields() { return serviceFields(this.un.params[this.ut?.service]) },
      utAdvancedFields() {
        const profiles = this.un.profiles[this.ut?.service] || []
        if (!profiles.length) return ADVANCED_FIELDS.filter(f => f.key !== 'profile')
        return ADVANCED_FIELDS.map(f => (f.key === 'profile' ? { ...f, type: 'choice', choices: profiles } : f))
      },
      utEpisodeOptions() {
        const codes = (this.ut?.titles || []).map(wantedCode).filter(Boolean)
        return codes.map((c, i) => ({ value: c, breakBefore: i > 0 && c.slice(0, 3) !== codes[i - 1].slice(0, 3) }))
      },
      utCombined() {
        const r = this.ut?.results || []
        if (!r.length) return null
        return summarize({ video: r.flatMap(x => x.tracks.video || []), audio: r.flatMap(x => x.tracks.audio || []),
          subtitles: r.flatMap(x => x.tracks.subtitles || []) })
      },
      utOriginalLangs() { return this.utCombined?.originalAudioLangs || [] },
      utVideoLadder() {
        const seen = new Map()
        for (const t of this.ut.results.flatMap(r => r.tracks.video || [])) if (!seen.has(videoKey(t))) seen.set(videoKey(t), t)
        return [...seen.values()].sort((a, b) => b.height - a.height || (b.bitrate || 0) - (a.bitrate || 0))
      },
      utVideoCodecs() { return distinct(this.utVideoLadder.map(t => t.codec_display || t.codec)) },
      utVideoRows() {
        const u = this.ut
        const rows = this.utVideoLadder.filter(t => {
          if (u.vRes !== 'all') { if (u.vRes === 'sd') { if (t.height >= 720) return false } else if (String(t.height) !== u.vRes) return false }
          if (u.vRange !== 'all' && rangeGroup(t) !== u.vRange) return false
          if (u.vCodec !== 'all' && (t.codec_display || t.codec) !== u.vCodec) return false
          return true
        })
        const byBitrate = (a, b) => (b.bitrate || 0) - (a.bitrate || 0)
        const byRes = (a, b) => b.width * b.height - a.width * a.height
        const [key, dir] = u.vSort.split('-')
        return [...rows].sort((a, b) => {
          if (key === 'default') return byRes(a, b) || byBitrate(a, b)
          const d = byBitrate(a, b)
          return (dir === 'asc' ? -d : d) || byRes(a, b)
        })
      },
      utAudioRows() {
        const u = this.ut, best = new Map()
        for (const t of u.results.flatMap(r => r.tracks.audio || [])) {
          const lang = t.language || '', cur = best.get(lang)
          if (!cur || Number(t.atmos) - Number(cur.atmos) > 0 || (t.channels || 0) > (cur.channels || 0) || (t.bitrate || 0) > (cur.bitrate || 0)) best.set(lang, t)
        }
        let rows = [...best.values()]
        if (!u.showAllLangs) rows = rows.filter(t => u.audioSel.includes(t.language || '') || ['en', 'th', ...this.utOriginalLangs].includes(base(t.language)))
        const q = u.audioSearch.toLowerCase().trim()
        return rows.filter(t => {
          if (u.audioQuick === 'th' && base(t.language) !== 'th') return false
          if (u.audioQuick === 'en' && base(t.language) !== 'en') return false
          if (u.audioQuick === 'atmos' && !t.atmos) return false
          if (q && !`${t.language} ${t.codec_display || t.codec}${t.atmos ? ' atmos' : ''}`.toLowerCase().includes(q)) return false
          return true
        }).sort((a, b) => Number(b.atmos) - Number(a.atmos) || Number(isOriginal(b)) - Number(isOriginal(a)) || (b.bitrate || 0) - (a.bitrate || 0))
      },
      utSubRows() {
        const u = this.ut, seen = new Map()
        for (const t of u.results.flatMap(r => r.tracks.subtitles || [])) {
          const k = `${t.language}-${t.forced}-${t.sdh}-${t.cc}`
          if (!seen.has(k)) seen.set(k, t)
        }
        let rows = [...seen.values()]
        if (!u.showAllLangs) rows = rows.filter(t => u.subSel.includes(t.language || '') || ['en', 'th', ...this.utOriginalLangs].includes(base(t.language)))
        const q = u.subSearch.toLowerCase().trim()
        return rows.filter(t => {
          if (u.subQuick === 'th' && base(t.language) !== 'th') return false
          if (u.subQuick === 'en' && base(t.language) !== 'en') return false
          if (u.subQuick === 'forced' && !t.forced) return false
          if (u.subQuick === 'sdh' && !t.sdh && !t.cc) return false
          if (q && !(t.language || '').toLowerCase().includes(q)) return false
          return true
        }).sort((a, b) => Number(b.forced) - Number(a.forced) || (a.language || '').localeCompare(b.language || ''))
      },
      utAudioLadder() {
        const seen = new Map()
        for (const t of this.ut.results.flatMap(r => r.tracks.audio || [])) if (!seen.has(audioKey(t))) seen.set(audioKey(t), t)
        return [...seen.values()].sort((a, b) => Number(b.atmos) - Number(a.atmos) || (b.channels || 0) - (a.channels || 0) || (b.bitrate || 0) - (a.bitrate || 0))
      },
      utVideoPicked() { return this.utVideoRows.find(t => videoKey(t) === this.ut.videoPick) || null },
      utRaw() {
        return this.ut.results.map(r => ({ code: r.code, name: r.title.name, video: (r.tracks.video || []).map(describeVideo),
          audio: (r.tracks.audio || []).map(describeAudio), subs: (r.tracks.subtitles || []).map(describeSubtitle) }))
      },
    },
    watch: {
      'un.service'() { this.unSave() },
      'un.proxy'() { this.unSave() },
      unProfiles(list) {
        // Preselect a named profile ("default" is just one of them, not a synthetic entry).
        if (list.length && !list.includes(this.un.profile)) this.un.profile = list.includes('default') ? 'default' : list[0]
      },
    },
    methods: {
      unVideoKey: videoKey, unAudioKey: audioKey, unRangeGroup: rangeGroup, unIsOriginal: isOriginal,
      unSave() { Shared.save('unshackle-browse', { service: this.un.service, proxy: this.un.proxy }) },
      async loadUnshackleServices() {
        try {
          const result = await this.api('/api/unshackle/services')
          Object.assign(this.un, { enabled: result.enabled, services: result.services || [], searchable: result.searchable || [],
            params: result.params || {}, profiles: result.profiles || {} })
          if (this.un.services.length && !this.un.services.includes(this.un.service))
            this.un.service = this.un.services.find(t => t === 'DSNP') || this.un.services[0]
        } catch (error) { this.fail(error) }
      },
      unPickService(tag) { this.un.service = tag; this.un.profile = ''; this.un.noProxy = false },
      unTransport() {
        return { profile: this.un.profile || undefined, proxy: this.un.proxy.trim() || undefined, no_proxy: this.un.noProxy || undefined }
      },
      async unSearch() {
        if (!this.unCanSubmit || !this.unCanSearch) return
        Object.assign(this.un, { searching: true, searchError: '' })
        try {
          this.un.results = (await this.api('/api/unshackle/search', { service: this.un.service, query: this.un.query.trim(), ...this.unTransport() })).results || []
        } catch (error) { this.un.searchError = error.message; this.un.results = null }
        finally { this.un.searching = false }
      },
      // Open a title by search result or by a pasted ID / URL (the title page's loader).
      async unOpen(id) {
        const seq = ++this.utSeq
        const ut = { ...blankTitle(), service: this.un.service, id }
        // The title page starts from the Browse transport (profile / proxy / no proxy).
        Object.assign(ut.advanced, { profile: this.un.profile, proxy: this.un.proxy.trim(), no_proxy: this.un.noProxy })
        ut.svcValues = blankValues(serviceFields(this.un.params[ut.service]))
        this.ut = ut
        this.view = 'title'
        let titles = [], error = ''
        try {
          titles = (await this.api('/api/unshackle/titles', { service: ut.service, title: id, ...this.unTransport() })).titles || []
        } catch (e) { error = e.message }
        if (seq !== this.utSeq) return
        Object.assign(this.ut, { titles, error, opening: false })
        // Series load only on "Refresh" (listing tracks per episode burns quota); movies load on open.
        if (!this.ut.error && this.ut.titles.length && !this.utEpisodic) this.utLoad([])
      },
      utToggleEp(code) {
        const sel = this.ut.epSel
        this.ut.epSel = sel.includes(code) ? sel.filter(c => c !== code) : this.utEpisodeOptions.map(o => o.value).filter(c => c === code || sel.includes(c))
      },
      utTrackBody(t) {
        const a = this.ut.advanced
        return {
          service: this.ut.service, title: this.ut.id, wanted: wantedCode(t) || undefined,
          // Same service options the download will use, so the listed tracks match what it resolves.
          svc: coerce(this.utSvcFields, this.ut.svcValues),
          profile: String(a.profile || '').trim() || undefined, proxy: String(a.proxy || '').trim() || undefined, no_proxy: a.no_proxy === true || undefined,
        }
      },
      // Load tracks for the selected episodes (or the first episode / the movie when none is selected).
      async utLoad(sel) {
        const ut = this.ut, seq = this.utSeq
        const targets = this.utEpisodic && sel.length ? ut.titles.filter(t => sel.includes(wantedCode(t))) : [ut.titles[0]]
        Object.assign(ut, { loading: true, trackError: '' })
        try {
          const results = await Promise.all(targets.map(async t => ({ code: wantedCode(t), title: t,
            tracks: await this.api('/api/unshackle/tracks', this.utTrackBody(t)) })))
          if (seq !== this.utSeq) return
          Object.assign(ut, { results, loadedSel: [...sel], qualitySel: [], rangeSel: [], audioSel: [], subSel: [], videoPick: null, audioPick: null })
          Object.assign(ut.advanced, { vbitrate: '', acodec: '', channels: '', abitrate: '' })
          ut.loading = false
          this.utMoreRanges(seq)
        } catch (error) {
          if (seq === this.utSeq) Object.assign(ut, { trackError: error.message, results: [], loading: false })
        }
      },
      // AMZN lists one range per manifest, so a title that came back all-SDR may still have HDR10 / DV:
      // fetch those too and merge (unshackle-ui shows only the default range for such services).
      async utMoreRanges(seq) {
        // this.ut.results, not the raw array: pushing into the reactive copy updates the table.
        const sdrOnly = this.ut.results.filter(r => (r.tracks.video || []).length && r.tracks.video.every(v => v.range === 'SDR'))
        if (!sdrOnly.length) return
        this.ut.more = true
        await Promise.all(sdrOnly.flatMap(r => ['HDR10', 'DV'].map(async range => {
          try {
            const extra = await this.api('/api/unshackle/tracks', { ...this.utTrackBody(r.title), range: [range] })
            if (seq !== this.utSeq) return
            const seen = new Set(r.tracks.video.map(videoKey))
            for (const v of extra.video || []) if (!seen.has(videoKey(v))) { seen.add(videoKey(v)); r.tracks.video.push(v) }
          } catch { /* that range does not exist for this title */ }
        })))
        if (seq === this.utSeq) this.ut.more = false
      },
      utToggleLang(key, lang) {
        if (!lang) return
        const set = this.ut[key]
        this.ut[key] = set.includes(lang) ? set.filter(l => l !== lang) : [...set, lang]
      },
      utRecommended(key, langs) {
        this.ut[key] = langs.filter(l => [...this.utOriginalLangs, 'en', 'th'].includes(base(l)))
      },
      // Pick (or unpick) one exact rung: quality (height) + its range + pinned bitrate. Empty = best available.
      utPickVideo(t) {
        const ut = this.ut
        if (!t || ut.videoPick === videoKey(t)) {
          Object.assign(ut, { videoPick: null, qualitySel: [], rangeSel: [] })
          ut.advanced.vbitrate = ''
        } else {
          Object.assign(ut, { videoPick: videoKey(t), qualitySel: [String(t.height)], rangeSel: [t.range] })
          ut.advanced.vbitrate = t.bitrate != null ? String(t.bitrate) : ''
        }
      },
      utPickAudio(t) {
        const ut = this.ut
        if (!t || ut.audioPick === audioKey(t)) { ut.audioPick = null; Object.assign(ut.advanced, { acodec: '', channels: '', abitrate: '' }) }
        else {
          ut.audioPick = audioKey(t)
          Object.assign(ut.advanced, { acodec: t.codec, channels: t.channels != null ? String(t.channels) : '', abitrate: t.bitrate != null ? String(t.bitrate) : '' })
        }
      },
      utQualityLabel(t) {
        return t.width >= 3840 || t.height >= 2160 ? '4K' : t.height >= 1080 || t.width >= 1920 ? '1080p' : t.height >= 720 ? '720p' : `${t.height}p`
      },
      utAudioLabel(t) { return [t.codec_display || t.codec, t.channels != null ? `${Number(t.channels).toFixed(1)}ch` : null].filter(Boolean).join(' · ') },
      utSetCount(fields, values) { return Object.keys(coerce(fields, values)).length },
      utGroups(fields) {
        const out = []
        for (const f of fields) {
          let g = out[out.length - 1]
          if (!g || g.name !== f.group) { g = { name: f.group, bools: [], inputs: [] }; out.push(g) }
          ;(f.type === 'bool' ? g.bools : g.inputs).push(f)
        }
        return out
      },
      // unshackle-ui buildDownloadRequest: selectors, then advanced, then service options (last wins).
      async utStart() {
        const ut = this.ut
        const wanted = ut.epSel.length ? ut.epSel : this.utEpisodeOptions.map(o => o.value)
        // No pick = the original language: the API's own default is "all", which would pull every dub.
        const aLang = ut.audioSel.length ? ut.audioSel : this.utOriginalLangs
        const request = {
          ...(wanted.length ? { wanted } : {}),
          ...(ut.qualitySel.length ? { quality: ut.qualitySel.map(Number) } : {}),
          ...(ut.rangeSel.length ? { range: ut.rangeSel } : {}),
          ...(aLang.length ? { a_lang: aLang } : {}),
          ...(ut.subSel.length ? { s_lang: ut.subSel } : {}),
          ...coerce(ADVANCED_FIELDS, ut.advanced),
          ...coerce(this.utSvcFields, ut.svcValues),
        }
        Object.assign(ut, { starting: true, startError: '' })
        try {
          const job = (await this.api('/api/unshackle/download', { service: ut.service, title: ut.id, name: this.utHeader, request })).job
          this.queued(job)
          this.view = 'queue'
        } catch (error) { ut.startError = error.message }
        finally { ut.starting = false }
      },
    },
  }
})()
