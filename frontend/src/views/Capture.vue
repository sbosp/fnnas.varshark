<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import {
  getCaptureStatus, startCapture, stopCapture,
  installCA, uninstallCA, caCertUrl, getLog,
} from '../api'

const st = ref({})
const msg = ref('')
const loading = ref(false)
const ports = ref('80,443')
const log = ref('')
let timer = null

async function refresh() {
  st.value = await getCaptureStatus()
}
onMounted(() => { refresh(); timer = setInterval(refresh, 3000) })
onUnmounted(() => clearInterval(timer))

function flash(t) { msg.value = t; setTimeout(() => { if (msg.value === t) msg.value = '' }, 4000) }

const canCapture = computed(() => st.value.binary && st.value.ca_in_system)

async function doInstallCA() {
  loading.value = true
  try {
    const r = await installCA()
    flash(r.ok ? '系统 CA 已安装 ✓' : '安装失败：' + (r.error || r.output))
    await refresh()
  } catch (e) { flash('安装失败：' + e) } finally { loading.value = false }
}
async function doUninstallCA() {
  if (!confirm('卸载系统 CA？卸载后将无法解密 HTTPS。')) return
  loading.value = true
  try { const r = await uninstallCA(); flash(r.ok ? '已卸载' : '失败'); await refresh() }
  finally { loading.value = false }
}
async function doStart() {
  loading.value = true
  try {
    const arr = ports.value.split(',').map(x => parseInt(x.trim())).filter(Boolean)
    const r = await startCapture(arr)
    if (r.ok) flash('抓包已启动 ✓')
    else if (r.need_ca) flash('请先安装系统 CA')
    else flash('启动失败：' + JSON.stringify(r.detail || r.error || r))
    await refresh()
  } catch (e) { flash('启动失败：' + e) } finally { loading.value = false }
}
async function doStop() {
  loading.value = true
  try { await stopCapture(); flash('抓包已停止'); await refresh() }
  finally { loading.value = false }
}
async function loadLog() {
  try { const r = await getLog('mitmdump', 200); log.value = r.log || '(空)' }
  catch (e) { log.value = String(e) }
}
</script>

<template>
  <div class="card">
    <h2>抓包引擎状态</h2>
    <div class="statgrid">
      <div><span class="muted">mitmdump 二进制</span><br><span class="tag" :class="st.binary ? 'ok' : 'off'">{{ st.binary ? '已安装' : '未安装' }}</span></div>
      <div><span class="muted">系统 CA</span><br><span class="tag" :class="st.ca_in_system ? 'ok' : 'off'">{{ st.ca_in_system ? '已装入' : '未装入' }}</span></div>
      <div><span class="muted">iptables 重定向</span><br><span class="tag" :class="st.iptables_active ? 'ok' : 'off'">{{ st.iptables_active ? '生效' : '未生效' }}</span></div>
      <div><span class="muted">抓包进程</span><br><span class="tag" :class="st.alive ? 'ok' : 'off'">{{ st.alive ? '抓取中' : '已停止' }}</span></div>
    </div>
    <div v-if="msg" class="msg">{{ msg }}</div>
  </div>

  <div class="card">
    <h2>① 系统 CA 证书（解密 HTTPS 必需）</h2>
    <p class="muted">
      抓包引擎会自动为每个 HTTPS 站点签发证书。要让 NAS 本机信任这些证书、从而解密出明文，
      必须把 RayShark 的根证书装入系统信任库（<code>/usr/local/share/ca-certificates/</code>）。
    </p>
    <div class="row">
      <button class="btn primary" @click="doInstallCA" :disabled="loading || st.ca_in_system">安装到系统</button>
      <button class="btn" @click="doUninstallCA" :disabled="loading || !st.ca_in_system">卸载</button>
      <a class="btn" :href="caCertUrl()" target="_blank">下载证书（装到其它设备）</a>
    </div>
  </div>

  <div class="card">
    <h2>② 抓包范围与启停</h2>
    <label class="field" style="max-width:320px">
      <span>抓取的目标端口（逗号分隔，抓 NAS 本机出站）</span>
      <input v-model="ports" placeholder="80,443">
    </label>
    <div class="row">
      <button class="btn primary" v-if="!st.alive" @click="doStart" :disabled="loading || !canCapture">开始抓包</button>
      <button class="btn danger" v-else @click="doStop" :disabled="loading">停止抓包</button>
      <span v-if="!canCapture" class="muted">
        {{ !st.binary ? '需先安装 mitmdump 二进制；' : '' }}{{ !st.ca_in_system ? '需先安装系统 CA。' : '' }}
      </span>
    </div>
    <p class="muted" style="margin-top:10px">
      抓取的是 <b>NAS 设备本机</b>发起的出站流量（OUTPUT 链重定向），不是浏览器/前端客户端的流量。
      内网与本地地址已自动排除。开始后到「流量」页查看实时明文。
    </p>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">mitmdump 日志</h2>
      <button class="btn" @click="loadLog">刷新日志</button>
    </div>
    <pre class="log">{{ log || '点「刷新日志」查看 mitmdump 输出' }}</pre>
  </div>
</template>

<style scoped>
.statgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 16px; line-height: 1.9; }
.msg { margin-top: 12px; padding: 8px 12px; background: #e8f3ff; color: #1868f0; border-radius: 6px; font-size: 13px; }
code { background: #f2f3f5; padding: 1px 5px; border-radius: 4px; font-size: 12px; }
.log {
  margin: 12px 0 0; padding: 12px; background: #1e1e1e; color: #d4d4d4; border-radius: 8px;
  font-family: ui-monospace, Menlo, monospace; font-size: 12px; max-height: 300px; overflow: auto;
  white-space: pre-wrap; word-break: break-all;
}
a.btn { text-decoration: none; display: inline-flex; align-items: center; }
</style>
