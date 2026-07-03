// 轻量全局 store（reactive），不引入 pinia。
// 管理：单例 WebSocket、实时流量缓冲、事件、连接状态。
import { reactive } from 'vue'
import { connectWS } from './api'

export const store = reactive({
  wsConnected: false,
  flows: [],          // 实时流量（最新在前）
  maxFlows: 2000,     // 前端最多保留
  lastEvent: null,
  events: [],         // 状态事件历史
})

let handle = null

export function startWS() {
  if (handle) return
  handle = connectWS({
    onOpen: () => { store.wsConnected = true },
    onClose: () => { store.wsConnected = false },
    onReady: () => {},
    onFlow: (flow) => {
      store.flows.unshift(flow)
      if (store.flows.length > store.maxFlows) store.flows.length = store.maxFlows
    },
    onEvent: (m) => {
      store.lastEvent = m
      store.events.unshift(m)
      if (store.events.length > 100) store.events.length = 100
    },
  })
}

export function stopWS() {
  if (handle) { handle.close(); handle = null; store.wsConnected = false }
}

export function clearLocalFlows() {
  store.flows = []
}
