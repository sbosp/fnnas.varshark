// API 基址：网关前缀。资源和接口都在此前缀下。
// import.meta.env.BASE_URL 由 vite base 注入，已含尾斜杠。
const BASE = import.meta.env.BASE_URL.replace(/\/$/, '')

export async function api(path, opts = {}) {
  const res = await fetch(`${BASE}/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText} ${text}`)
  }
  const ct = res.headers.get('content-type') || ''
  return ct.includes('application/json') ? res.json() : res.text()
}

const jbody = (data) => ({ body: JSON.stringify(data || {}) })

// ---- 基础 ----
export const getPing = () => api('/ping')
export const getWhoami = () => api('/whoami')
export const getSystem = () => api('/system')
export const getOverview = () => api('/overview')

// ---- 节点 ----
export const listNodes = () => api('/nodes')
export const createNode = (node) => api('/nodes', { method: 'POST', ...jbody(node) })
export const importNodes = (link) => api('/nodes/import', { method: 'POST', ...jbody({ link }) })
export const updateNode = (id, node) => api(`/nodes/${id}`, { method: 'PUT', ...jbody(node) })
export const deleteNode = (id) => api(`/nodes/${id}`, { method: 'DELETE' })
export const testNode = (id) => api(`/nodes/${id}/test`, { method: 'POST' })

// ---- 代理 ----
export const getProxyStatus = () => api('/proxy/status')
export const startProxy = (nodeId) => api('/proxy/start', { method: 'POST', ...jbody({ node_id: nodeId }) })
export const stopProxy = () => api('/proxy/stop', { method: 'POST' })

// ---- 抓包 ----
export const getCaptureStatus = () => api('/capture/status')
export const startCapture = (ports) => api('/capture/start', { method: 'POST', ...jbody({ ports }) })
export const stopCapture = () => api('/capture/stop', { method: 'POST' })
export const installCA = () => api('/capture/ca/install', { method: 'POST' })
export const uninstallCA = () => api('/capture/ca/uninstall', { method: 'POST' })
export const caCertUrl = () => `${BASE}/api/capture/ca/cert`

// ---- 流量 ----
export const listFlows = (since = 0) => api(`/flows?since=${since}`)
export const clearFlows = () => api('/flows/clear', { method: 'POST' })

// ---- 进程日志 ----
export const getLog = (name, n = 200) => api(`/logs/${name}?n=${n}`)

// ---- WebSocket ----
export function connectWS({ onFlow, onEvent, onReady, onOpen, onClose } = {}) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const url = `${proto}://${location.host}${BASE}/api/ws`
  let ws = null
  let closedByUser = false
  let retry = 0

  function open() {
    ws = new WebSocket(url)
    ws.onopen = () => { retry = 0; onOpen && onOpen() }
    ws.onmessage = (ev) => {
      let m
      try { m = JSON.parse(ev.data) } catch { return }
      if (m.type === 'flow') onFlow && onFlow(m.data)
      else if (m.type === 'event') onEvent && onEvent(m)
      else if (m.type === 'ready') onReady && onReady(m.data)
    }
    ws.onclose = () => {
      onClose && onClose()
      if (!closedByUser) {
        retry = Math.min(retry + 1, 6)
        setTimeout(open, retry * 1000)
      }
    }
    ws.onerror = () => { try { ws.close() } catch {} }
  }
  open()
  return { close() { closedByUser = true; try { ws && ws.close() } catch {} } }
}
