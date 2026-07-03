import { createRouter, createWebHashHistory } from 'vue-router'
import Dashboard from './views/Dashboard.vue'
import Nodes from './views/Nodes.vue'
import Capture from './views/Capture.vue'
import Flows from './views/Flows.vue'

// 用 hash 路由：网关 iframe 下无需后端配合前端路由，最稳。
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    { path: '/dashboard', name: 'dashboard', component: Dashboard, meta: { title: '仪表盘' } },
    { path: '/nodes', name: 'nodes', component: Nodes, meta: { title: '代理节点' } },
    { path: '/capture', name: 'capture', component: Capture, meta: { title: '抓包' } },
    { path: '/flows', name: 'flows', component: Flows, meta: { title: '流量' } },
  ],
})
