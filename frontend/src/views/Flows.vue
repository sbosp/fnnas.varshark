<script setup>
import { ref, computed } from 'vue'
import { store, clearLocalFlows } from '../store'
import { clearFlows } from '../api'

const selected = ref(null)
const filter = ref('')
const methodFilter = ref('')
const statusFilter = ref('')
const paused = ref(false)
const detailTab = ref('resp')

// 暂停时用快照，避免列表跳动
const snapshot = ref([])
function togglePause() {
  paused.value = !paused.value
  snapshot.value = paused.value ? [...store.flows] : []
}

const source = computed(() => (paused.value ? snapshot.value : store.flows))

const filtered = computed(() => {
  const f = filter.value.toLowerCase()
  return source.value.filter((x) => {
    if (f && !((x.url || '').toLowerCase().includes(f) || (x.host || '').toLowerCase().includes(f))) return false
    if (methodFilter.value && x.method !== methodFilter.value) return false
    if (statusFilter.value) {
      const s = String(x.status || 0)
      if (statusFilter.value === '2xx' && !s.startsWith('2')) return false
      if (statusFilter.value === '3xx' && !s.startsWith('3')) return false
      if (statusFilter.value === '4xx' && !s.startsWith('4')) return false
      if (statusFilter.value === '5xx' && !s.startsWith('5')) return false
      if (statusFilter.value === 'err' && x.status !== 0) return false
    }
    return true
  })
})

function statusClass(s) {
  if (!s) return 'err'
  if (s >= 500) return 's5'
  if (s >= 400) return 's4'
  if (s >= 300) return 's3'
  return 's2'
}

async function doClear() {
  await clearFlows(); clearLocalFlows(); selected.value = null
  if (paused.value) snapshot.value = []
}

function fmtHeaders(h) {
  if (!h) return ''
  return Object.entries(h).map(([k, v]) => `${k}: ${v}`).join('\n')
}
function bodyText(b) {
  if (!b) return ''
  if (b.binary) return `[二进制内容 ${b.size} 字节]`
  return b.text + (b.truncated ? `\n\n… (已截断，完整 ${b.size} 字节)` : '')
}

// 导出 HAR
function exportHAR() {
  const entries = source.value.slice().reverse().map((f) => ({
    startedDateTime: new Date((f.ts || 0) * 1000).toISOString(),
    time: f.duration_ms || 0,
    request: {
      method: f.method, url: f.url, httpVersion: f.http_version || 'HTTP/1.1',
      headers: Object.entries(f.req_headers || {}).map(([name, value]) => ({ name, value })),
      queryString: [], cookies: [], headersSize: -1,
      bodySize: f.req_body?.size || 0,
      postData: f.req_body?.text ? { mimeType: '', text: f.req_body.text } : undefined,
    },
    response: {
      status: f.status || 0, statusText: '', httpVersion: 'HTTP/1.1',
      headers: Object.entries(f.resp_headers || {}).map(([name, value]) => ({ name, value })),
      cookies: [], headersSize: -1, bodySize: f.resp_body?.size || 0,
      content: { size: f.resp_body?.size || 0, mimeType: f.content_type || '', text: f.resp_body?.text || '' },
      redirectURL: '',
    },
    cache: {}, timings: { send: 0, wait: f.duration_ms || 0, receive: 0 },
  }))
  const har = { log: { version: '1.2', creator: { name: 'RayShark', version: '1.0' }, entries } }
  const blob = new Blob([JSON.stringify(har, null, 2)], { type: 'application/json' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `rayshark-${Date.now()}.har`
  a.click()
  URL.revokeObjectURL(a.href)
}
</script>

<template>
  <div class="flows-wrap">
    <div class="toolbar card">
      <div class="row">
        <input v-model="filter" placeholder="过滤 URL / Host" style="width:220px">
        <select v-model="methodFilter" style="width:100px">
          <option value="">全部方法</option>
          <option>GET</option><option>POST</option><option>PUT</option><option>DELETE</option>
        </select>
        <select v-model="statusFilter" style="width:110px">
          <option value="">全部状态</option>
          <option value="2xx">2xx</option><option value="3xx">3xx</option>
          <option value="4xx">4xx</option><option value="5xx">5xx</option><option value="err">错误</option>
        </select>
      </div>
      <div class="row">
        <span class="muted">{{ filtered.length }} / {{ source.length }} 条</span>
        <button class="btn" @click="togglePause">{{ paused ? '继续' : '暂停' }}</button>
        <button class="btn" @click="exportHAR" :disabled="!source.length">导出 HAR</button>
        <button class="btn danger" @click="doClear" :disabled="!source.length">清空</button>
      </div>
    </div>

    <div class="split">
      <div class="list card">
        <table>
          <thead><tr>
            <th style="width:52px">方法</th><th style="width:48px">状态</th>
            <th>Host / Path</th><th style="width:64px">耗时</th>
          </tr></thead>
          <tbody>
            <tr v-for="f in filtered" :key="f.seq" @click="selected = f"
              :class="{ sel: selected && selected.seq === f.seq }">
              <td><span class="m">{{ f.method }}</span></td>
              <td><span class="st" :class="statusClass(f.status)">{{ f.status || 'ERR' }}</span></td>
              <td class="url">
                <span class="host">{{ f.host }}</span>
                <span class="path">{{ f.path }}</span>
              </td>
              <td class="muted">{{ f.duration_ms ? f.duration_ms + 'ms' : '–' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="!filtered.length" class="empty muted">
          {{ source.length ? '无匹配结果' : '暂无流量。到「抓包」页开始抓包后，NAS 本机的请求会实时出现在这里。' }}
        </div>
      </div>

      <div class="detail card" v-if="selected">
        <div class="d-head">
          <span class="st" :class="statusClass(selected.status)">{{ selected.status || 'ERR' }}</span>
          <span class="m">{{ selected.method }}</span>
          <span class="d-url mono">{{ selected.url }}</span>
          <button class="btn close" @click="selected = null">✕</button>
        </div>
        <div v-if="selected.error" class="err-box">错误：{{ selected.error }}</div>
        <div class="tabs">
          <button :class="{ on: detailTab==='resp' }" @click="detailTab='resp'">响应</button>
          <button :class="{ on: detailTab==='req' }" @click="detailTab='req'">请求</button>
        </div>
        <div v-if="detailTab==='resp'">
          <h3>响应头</h3><pre class="mono">{{ fmtHeaders(selected.resp_headers) }}</pre>
          <h3>响应体 <span class="muted" v-if="selected.content_type">({{ selected.content_type }})</span></h3>
          <pre class="mono body">{{ bodyText(selected.resp_body) }}</pre>
        </div>
        <div v-else>
          <h3>请求头</h3><pre class="mono">{{ fmtHeaders(selected.req_headers) }}</pre>
          <h3>请求体</h3><pre class="mono body">{{ bodyText(selected.req_body) || '(无)' }}</pre>
        </div>
      </div>
      <div class="detail card empty-detail muted" v-else>点击左侧任一请求查看明文详情</div>
    </div>
  </div>
</template>

<style scoped>
.flows-wrap { display: flex; flex-direction: column; height: 100%; gap: 12px; }
.toolbar { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; flex: 0 0 auto; }
.split { display: flex; gap: 12px; flex: 1; min-height: 0; }
.list { flex: 1; overflow: auto; padding: 0; min-width: 0; }
.list table { width: 100%; }
.list th { position: sticky; top: 0; background: #fff; z-index: 1; }
.list tr { cursor: pointer; }
.list tbody tr:hover, .list tr:hover { background: #f6fbff; }
.list tr.sel { background: #e8f3ff; }
.url .host { font-weight: 500; }
.url .path { color: #86909c; margin-left: 4px; font-size: 12px; }
.m { font-size: 11px; font-weight: 700; color: #1868f0; }
.st { font-size: 11px; font-weight: 700; padding: 1px 6px; border-radius: 4px; }
.st.s2 { background: #e8ffea; color: #00b42a; }
.st.s3 { background: #fff7e8; color: #ff7d00; }
.st.s4 { background: #fff1f0; color: #f53f3f; }
.st.s5 { background: #fbe8ff; color: #d91ad9; }
.st.err { background: #f2f3f5; color: #86909c; }
.detail { width: 46%; flex: 0 0 46%; overflow: auto; }
.empty-detail { display: flex; align-items: center; justify-content: center; }
.d-head { display: flex; align-items: center; gap: 8px; padding-bottom: 12px; border-bottom: 1px solid #f0f1f3; }
.d-url { flex: 1; font-size: 12px; word-break: break-all; }
.close { padding: 2px 8px; }
.tabs { display: flex; gap: 6px; margin: 12px 0; }
.tabs button { border: none; background: #f2f3f5; padding: 6px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.tabs button.on { background: #1868f0; color: #fff; }
.detail h3 { font-size: 13px; margin: 14px 0 6px; color: #4e5969; }
.mono { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
pre.mono { background: #fafbfc; border: 1px solid #f0f1f3; border-radius: 6px; padding: 10px; white-space: pre-wrap; word-break: break-all; margin: 0; }
pre.body { max-height: 340px; overflow: auto; }
.err-box { margin-top: 10px; padding: 8px 12px; background: #fff1f0; color: #f53f3f; border-radius: 6px; font-size: 13px; }
.empty { padding: 30px; text-align: center; }
</style>
