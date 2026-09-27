// Bilibili account status + QR-code login, shared by the main and transfer
// pages. Register with app.component('bili-account', BiliAccount) and pass the
// page's token-aware `api(path, body)` helper; messages come back as
// `notify(text, kind)` events.
const BiliAccount = {
  props: { api: { type: Function, required: true } },
  emits: ['notify'],
  data() { return {
    auth: null, loading: false, restarting: false,
    qr: { open: false, key: '', svg: '', state: '', expiresAt: 0 }, timer: null,
  } },
  computed: {
    needsRestart() { return !!(this.auth?.valid && this.auth.bili23 && !this.auth.bili23.logged_in) },
    tone() {
      const auth = this.auth
      if (!auth) return ''
      if (!auth.config || auth.valid === false) return 'bad'
      return auth.valid && !this.needsRestart ? 'ok' : 'warn'
    },
    text() {
      const auth = this.auth
      if (!auth) return this.loading ? 'กำลังตรวจสถานะบัญชี Bilibili…' : 'ยังไม่ได้ตรวจสถานะบัญชี Bilibili'
      if (!auth.config) return 'ไม่พบ config.json ของ Bili23 เปิด Bili23 อย่างน้อยหนึ่งครั้ง หรือตั้ง BILI_CONFIG'
      if (auth.valid) return `เข้าสู่ระบบ Bilibili เป็น ${auth.username}${auth.vip ? ` · ${auth.vip}` : ''}`
      if (auth.saved && auth.valid === false) return 'cookie Bilibili ที่บันทึกไว้หมดอายุแล้ว ต้องเข้าสู่ระบบใหม่'
      if (auth.saved) return `มี cookie ของ uid ${auth.uid} แต่ตรวจกับ Bilibili ไม่ได้`
      return 'ยังไม่ได้เข้าสู่ระบบ Bilibili'
    },
    qrText() {
      return {
        waiting: 'รอสแกน QR…', scanned: 'สแกนแล้ว กดยืนยันบนมือถือ', expired: 'QR หมดอายุแล้ว',
        success: 'เข้าสู่ระบบสำเร็จ', error: 'Bilibili ตอบกลับไม่ถูกต้อง ลองสร้าง QR ใหม่',
      }[this.qr.state] || 'กำลังสร้าง QR…'
    },
  },
  methods: {
    say(text, kind = 'info') { this.$emit('notify', text, kind) },
    async load() {
      this.loading = true
      try { this.auth = await this.api('/api/bili/login/status') } catch (error) { this.say(error.message, 'error') } finally { this.loading = false }
    },
    async start() {
      this.stopPolling()
      this.qr = { open: true, key: '', svg: '', state: '', expiresAt: 0 }
      try {
        const result = await this.api('/api/bili/login/start')
        const code = qrcode(0, 'M')
        code.addData(result.url)
        code.make()
        this.qr = { open: true, key: result.key, svg: code.createSvgTag({ cellSize: 6, margin: 3, scalable: true }), state: 'waiting', expiresAt: Date.now() + 180000 }
        this.timer = setInterval(() => this.poll(), 2000)
      } catch (error) { this.qr.open = false; this.say(error.message, 'error') }
    },
    async poll() {
      const key = this.qr.key
      if (!key || !this.qr.open) return
      if (Date.now() > this.qr.expiresAt) { this.qr.state = 'expired'; this.stopPolling(); return }
      try {
        const result = await this.api('/api/bili/login/poll', { key })
        if (key !== this.qr.key) return
        this.qr.state = result.state
        if (result.state === 'expired' || result.state === 'error') this.stopPolling()
        if (result.state === 'success') {
          this.close()
          await this.load()
          this.say(this.needsRestart ? 'เข้าสู่ระบบแล้ว รีสตาร์ต Bili23 เพื่อเริ่มใช้บัญชีนี้' : 'เข้าสู่ระบบ Bilibili แล้ว', 'ok')
        }
      } catch (error) { this.stopPolling(); this.qr.state = 'error'; this.say(error.message, 'error') }
    },
    stopPolling() { clearInterval(this.timer); this.timer = null },
    close() { this.stopPolling(); this.qr = { open: false, key: '', svg: '', state: '', expiresAt: 0 } },
    async logout() {
      if (!confirm('ลบ cookie ของบัญชี Bilibili ออกจาก Bili23 หรือไม่?')) return
      try { await this.api('/api/bili/logout'); await this.load(); this.say('ออกจากระบบแล้ว รีสตาร์ต Bili23 เพื่อให้มีผล', 'ok') } catch (error) { this.say(error.message, 'error') }
    },
    async restart() {
      if (!confirm('รีสตาร์ต Bili23 ตอนนี้หรือไม่?')) return
      this.restarting = true
      try {
        await this.api('/api/bili/restart')
        this.say('รีสตาร์ต Bili23 แล้ว กำลังตรวจสถานะ…', 'ok')
        await new Promise(resolve => setTimeout(resolve, 4000))
        await this.load()
      } catch (error) { this.say(error.message, 'error') } finally { this.restarting = false }
    },
    onKey(event) { if (event.key === 'Escape' && this.qr.open) this.close() },
  },
  mounted() { this.load(); window.addEventListener('keydown', this.onKey) },
  beforeUnmount() { this.stopPolling(); window.removeEventListener('keydown', this.onKey) },
  template: `
    <div class="bl-account" :class="tone">
      <div class="bl-line"><span class="bl-spinner" v-if="loading && !auth"></span><b>{{text}}</b></div>
      <p class="bl-hint" v-if="auth && !auth.valid && auth.config">ถ้าไม่ล็อกอิน จะดาวน์โหลดได้เฉพาะคุณภาพต่ำ ส่วนเนื้อหา VIP จะดาวน์โหลดไม่ได้</p>
      <p class="bl-hint warn" v-if="needsRestart">Bili23 ยังใช้ cookie ชุดเดิมอยู่ ต้องรีสตาร์ต Bili23 ก่อน ระบบจึงจะใช้บัญชีนี้ดาวน์โหลด</p>
      <p class="bl-hint warn" v-if="auth?.bili23_error" :title="auth.bili23_error">ติดต่อ Bili23 ไม่ได้ (ยังไม่ได้เปิด Bili23 หรือ MCP)</p>
      <div class="bl-actions" v-if="auth?.config">
        <button @click="start">{{auth.valid ? 'เปลี่ยนบัญชี' : 'เข้าสู่ระบบด้วย QR'}}</button>
        <button v-if="needsRestart" @click="restart" :disabled="restarting">{{restarting ? 'กำลังรีสตาร์ต…' : 'รีสตาร์ต Bili23'}}</button>
        <button class="ghost" v-if="auth.saved" @click="logout">ออกจากระบบ</button>
        <button class="ghost" @click="load" :disabled="loading">ตรวจอีกครั้ง</button>
      </div>
    </div>
    <Teleport to="body">
      <div class="bl-scrim" v-if="qr.open" @click="close"></div>
      <div class="bl-dialog" v-if="qr.open" role="dialog" aria-label="เข้าสู่ระบบ Bilibili ด้วย QR">
        <h2>เข้าสู่ระบบ Bilibili</h2>
        <div class="bl-qr" :class="{faded: qr.state === 'expired' || qr.state === 'scanned'}">
          <div v-if="qr.svg" v-html="qr.svg"></div>
          <span class="bl-spinner" v-else></span>
          <div class="bl-overlay" v-if="qr.state === 'expired'"><button @click="start">สร้าง QR ใหม่</button></div>
          <div class="bl-overlay ok" v-if="qr.state === 'scanned'">✓ สแกนแล้ว</div>
        </div>
        <p class="bl-status" :class="qr.state">{{qrText}}</p>
        <ol class="bl-steps">
          <li>เปิดแอป Bilibili บนมือถือ แล้วไปที่ “我的” (Mine)</li>
          <li>แตะไอคอนสแกนมุมขวาบน แล้วสแกน QR นี้</li>
          <li>กดยืนยันการเข้าสู่ระบบบนมือถือ</li>
        </ol>
        <button class="ghost" @click="close">ปิด</button>
      </div>
    </Teleport>`,
}
