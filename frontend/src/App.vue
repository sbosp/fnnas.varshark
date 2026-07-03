<script setup>
import { onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { startWS, stopWS, store } from './store'

const route = useRoute()
const nav = [
  { to: '/dashboard', icon: '📊', label: '仪表盘' },
  { to: '/nodes', icon: '🛰️', label: '代理节点' },
  { to: '/capture', icon: '🎯', label: '抓包' },
  { to: '/flows', icon: '🌊', label: '流量' },
]

onMounted(() => startWS())
onUnmounted(() => stopWS())
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">
        <span class="logo">🦈</span>
        <span class="name">RayShark</span>
      </div>
      <nav>
        <RouterLink v-for="n in nav" :key="n.to" :to="n.to" class="navitem"
          :class="{ active: route.path === n.to }">
          <span class="ni-icon">{{ n.icon }}</span>
          <span class="ni-label">{{ n.label }}</span>
        </RouterLink>
      </nav>
      <div class="ws-status" :class="{ on: store.wsConnected }">
        <span class="dot"></span>
        {{ store.wsConnected ? '实时连接' : '未连接' }}
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <h1>{{ route.meta.title || 'RayShark' }}</h1>
        <div class="hint">调试：URL 加 <code>?debug=1</code> 开启 vConsole</div>
      </header>
      <div class="content">
        <RouterView />
      </div>
    </div>
  </div>
</template>

<style>
* { box-sizing: border-box; }
html, body, #app { height: 100%; margin: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', sans-serif;
  background: #f5f6f8; color: #1f2329;
}
.layout { display: flex; height: 100%; }
.sidebar {
  width: 200px; flex: 0 0 200px; background: #fff; border-right: 1px solid #e5e6eb;
  display: flex; flex-direction: column; padding: 16px 12px;
}
.brand { display: flex; align-items: center; gap: 8px; padding: 4px 8px 20px; }
.brand .logo { font-size: 24px; }
.brand .name { font-size: 17px; font-weight: 700; }
nav { display: flex; flex-direction: column; gap: 4px; flex: 1; }
.navitem {
  display: flex; align-items: center; gap: 10px; padding: 9px 12px;
  border-radius: 8px; text-decoration: none; color: #4e5969; font-size: 14px;
}
.navitem:hover { background: #f2f3f5; }
.navitem.active { background: #e8f3ff; color: #1868f0; font-weight: 600; }
.ni-icon { font-size: 16px; }
.ws-status {
  display: flex; align-items: center; gap: 7px; font-size: 12px; color: #86909c;
  padding: 8px 12px; border-top: 1px solid #f0f1f3;
}
.ws-status .dot { width: 8px; height: 8px; border-radius: 50%; background: #c9cdd4; }
.ws-status.on { color: #00b42a; }
.ws-status.on .dot { background: #00b42a; box-shadow: 0 0 0 3px rgba(0,180,42,.15); }

.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.topbar {
  height: 56px; flex: 0 0 56px; display: flex; align-items: center;
  justify-content: space-between; padding: 0 24px; background: #fff;
  border-bottom: 1px solid #e5e6eb;
}
.topbar h1 { font-size: 17px; margin: 0; }
.topbar .hint { font-size: 12px; color: #a9aeb8; }
.topbar code { background: #f2f3f5; padding: 1px 5px; border-radius: 4px; color: #1868f0; }
.content { flex: 1; overflow: auto; padding: 20px 24px; }

/* 通用组件样式 */
.card { background: #fff; border: 1px solid #e5e6eb; border-radius: 10px; padding: 18px; }
.card + .card { margin-top: 16px; }
.card h2 { margin: 0 0 14px; font-size: 15px; }
.btn {
  border: 1px solid #e5e6eb; background: #fff; border-radius: 6px; padding: 6px 14px;
  cursor: pointer; font-size: 13px; color: #1f2329;
}
.btn:hover { border-color: #4080ff; color: #4080ff; }
.btn.primary { background: #1868f0; color: #fff; border-color: #1868f0; }
.btn.primary:hover { background: #0e5ad6; color: #fff; }
.btn.danger { color: #f53f3f; border-color: #ffccc7; }
.btn.danger:hover { background: #fff1f0; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.muted { color: #86909c; font-size: 13px; }
.tag { font-size: 11px; padding: 2px 8px; border-radius: 10px; background: #f2f3f5; color: #4e5969; }
.tag.ok { background: #e8ffea; color: #00b42a; }
.tag.off { background: #fff1f0; color: #f53f3f; }
input, textarea, select {
  font: inherit; padding: 7px 10px; border: 1px solid #e5e6eb; border-radius: 6px;
  outline: none; width: 100%;
}
input:focus, textarea:focus { border-color: #4080ff; }
label.field { display: block; margin-bottom: 10px; }
label.field > span { display: block; font-size: 12px; color: #86909c; margin-bottom: 4px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #f0f1f3; }
th { color: #86909c; font-weight: 500; }
</style>
