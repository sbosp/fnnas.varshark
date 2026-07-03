<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { getOverview } from '../api'
import { store } from '../store'

const ov = ref(null)
const err = ref('')
let timer = null

async function load() {
  try { ov.value = await getOverview(); err.value = '' }
  catch (e) { err.value = String(e) }
}
onMounted(() => { load(); timer = setInterval(load, 3000) })
onUnmounted(() => clearInterval(timer))

const proxyOn = computed(() => ov.value?.proxy?.alive)
const capOn = computed(() => ov.value?.capture?.alive)
</script>

<template>
  <div v-if="err" class="card"><span class="tag off">连接失败</span> {{ err }}</div>

  <div class="grid">
    <div class="card stat">
      <div class="stat-h">代理状态</div>
      <div class="stat-v">
        <span class="tag" :class="proxyOn ? 'ok' : 'off'">{{ proxyOn ? '运行中' : '已停止' }}</span>
      </div>
      <div class="muted" v-if="ov?.proxy">
        SOCKS :{{ ov.proxy.socks_port }} · HTTP :{{ ov.proxy.http_port }}<br>
        二进制 {{ ov.proxy.binary ? '✓' : '✗ 未安装' }}
      </div>
    </div>

    <div class="card stat">
      <div class="stat-h">抓包状态</div>
      <div class="stat-v">
        <span class="tag" :class="capOn ? 'ok' : 'off'">{{ capOn ? '抓取中' : '已停止' }}</span>
      </div>
      <div class="muted" v-if="ov?.capture">
        端口 {{ (ov.capture.ports || []).join(', ') }}<br>
        CA {{ ov.capture.ca_in_system ? '已装入系统 ✓' : '未装入 ✗' }} ·
        iptables {{ ov.capture.iptables_active ? '生效' : '未生效' }}
      </div>
    </div>

    <div class="card stat">
      <div class="stat-h">节点数</div>
      <div class="stat-v big">{{ ov?.node_count ?? '–' }}</div>
      <div class="muted">当前激活：{{ ov?.active_node_id ?? '无' }}</div>
    </div>

    <div class="card stat">
      <div class="stat-h">已捕获流量</div>
      <div class="stat-v big">{{ ov?.flow_count ?? 0 }}</div>
      <div class="muted">实时连接 {{ store.wsConnected ? '在线' : '离线' }} · WS 客户端 {{ ov?.ws_clients ?? 0 }}</div>
    </div>
  </div>

  <div class="card">
    <h2>最近事件</h2>
    <table v-if="store.events.length">
      <thead><tr><th style="width:160px">时间</th><th style="width:140px">事件</th><th>详情</th></tr></thead>
      <tbody>
        <tr v-for="(e, i) in store.events.slice(0, 8)" :key="i">
          <td>{{ new Date(e.ts * 1000).toLocaleTimeString() }}</td>
          <td><span class="tag">{{ e.event }}</span></td>
          <td class="muted">{{ JSON.stringify(e.data) }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="muted">暂无事件。启停代理/抓包后会在此显示。</div>
  </div>

  <div class="card">
    <h2>快速开始</h2>
    <ol class="muted" style="line-height:2;padding-left:18px;margin:0">
      <li>到 <b>代理节点</b> 添加 VMess 节点（支持粘贴 vmess:// 链接），启动代理</li>
      <li>到 <b>抓包</b> 页安装系统 CA 证书（解密 HTTPS 必需），然后开始抓包</li>
      <li>到 <b>流量</b> 页实时查看 NAS 本机出站请求的明文内容</li>
    </ol>
  </div>
</template>

<style scoped>
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; margin-bottom: 16px; }
.stat { display: flex; flex-direction: column; gap: 8px; }
.stat-h { font-size: 13px; color: #86909c; }
.stat-v { font-size: 15px; }
.stat-v.big { font-size: 30px; font-weight: 700; color: #1f2329; }
</style>
