<script setup>
import { ref, onMounted } from 'vue'
import {
  listNodes, createNode, importNodes, deleteNode, testNode,
  startProxy, stopProxy, getProxyStatus,
} from '../api'

const nodes = ref([])
const activeId = ref(null)
const proxy = ref({ alive: false })
const loading = ref(false)
const msg = ref('')
const testing = ref({})   // id -> result

// 导入
const importText = ref('')
const showImport = ref(false)
// 手动新增
const showAdd = ref(false)
const form = ref(blankForm())
function blankForm() {
  return { name: '', address: '', port: 443, uuid: '', alter_id: 0,
    security: 'auto', network: 'tcp', ws_path: '', ws_host: '', tls: 0, sni: '' }
}

async function refresh() {
  const d = await listNodes()
  nodes.value = d.nodes
  activeId.value = d.active_node_id
  proxy.value = await getProxyStatus()
}
onMounted(refresh)

function flash(t) { msg.value = t; setTimeout(() => { if (msg.value === t) msg.value = '' }, 3000) }

async function doImport() {
  if (!importText.value.trim()) return
  loading.value = true
  try {
    const r = await importNodes(importText.value)
    flash(`导入成功 ${r.created.length} 个${r.errors.length ? '，失败 ' + r.errors.length : ''}`)
    importText.value = ''; showImport.value = false
    await refresh()
  } catch (e) { flash('导入失败：' + e) } finally { loading.value = false }
}

async function doAdd() {
  if (!form.value.address || !form.value.uuid) { flash('地址与 UUID 必填'); return }
  loading.value = true
  try {
    await createNode(form.value)
    flash('已添加'); showAdd.value = false; form.value = blankForm()
    await refresh()
  } catch (e) { flash('添加失败：' + e) } finally { loading.value = false }
}

async function doDelete(n) {
  if (!confirm(`删除节点「${n.name}」？`)) return
  await deleteNode(n.id); flash('已删除'); await refresh()
}

async function doTest(n) {
  testing.value = { ...testing.value, [n.id]: { loading: true } }
  try {
    const r = await testNode(n.id)
    testing.value = { ...testing.value, [n.id]: r }
  } catch (e) {
    testing.value = { ...testing.value, [n.id]: { ok: false, error: String(e) } }
  }
}

async function doActivate(n) {
  loading.value = true
  try {
    const r = await startProxy(n.id)
    if (r.ok) {
      const g = r.global && r.global.ok && r.global.global
      flash(g ? `已切换到「${n.name}」，全局代理已接管本机出站 ✓`
              : `已切换到「${n.name}」（全局接管未生效，仅本地端口可用）`)
    } else flash('启动失败：' + (r.error || JSON.stringify(r)))
    await refresh()
  } catch (e) { flash('启动失败：' + e) } finally { loading.value = false }
}

async function doStop() {
  loading.value = true
  try { await stopProxy(); flash('代理已停止'); await refresh() }
  finally { loading.value = false }
}
</script>

<template>
  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div class="row">
        <span class="tag" :class="proxy.alive ? 'ok' : 'off'">
          代理{{ proxy.alive ? '运行中' : '已停止' }}
        </span>
        <span v-if="proxy.alive" class="tag" :class="proxy.global_active ? 'ok' : 'off'">
          全局接管{{ proxy.global_active ? '生效' : '未生效' }}
        </span>
        <span class="muted" v-if="!proxy.binary">⚠️ v2ray 二进制未安装（打包时由 build.sh 放入）</span>
      </div>
      <div class="row">
        <button class="btn" @click="showImport = !showImport">导入 vmess://</button>
        <button class="btn" @click="showAdd = !showAdd">手动添加</button>
        <button class="btn danger" v-if="proxy.alive" @click="doStop" :disabled="loading">停止代理</button>
      </div>
    </div>
    <div v-if="msg" class="msg">{{ msg }}</div>

    <div v-if="showImport" class="panel">
      <label class="field">
        <span>粘贴 vmess:// 分享链接（可多行批量导入）</span>
        <textarea v-model="importText" rows="4" placeholder="vmess://eyJ2Ijoi..."></textarea>
      </label>
      <button class="btn primary" @click="doImport" :disabled="loading">导入</button>
    </div>

    <div v-if="showAdd" class="panel">
      <div class="form-grid">
        <label class="field"><span>备注名</span><input v-model="form.name" placeholder="香港节点"></label>
        <label class="field"><span>地址</span><input v-model="form.address" placeholder="hk.example.com"></label>
        <label class="field"><span>端口</span><input v-model.number="form.port" type="number"></label>
        <label class="field"><span>UUID</span><input v-model="form.uuid" placeholder="xxxxxxxx-...."></label>
        <label class="field"><span>alterId</span><input v-model.number="form.alter_id" type="number"></label>
        <label class="field"><span>加密</span>
          <select v-model="form.security"><option>auto</option><option>aes-128-gcm</option><option>chacha20-poly1305</option><option>none</option></select>
        </label>
        <label class="field"><span>传输</span>
          <select v-model="form.network"><option>tcp</option><option>ws</option></select>
        </label>
        <label class="field" v-if="form.network==='ws'"><span>WS Path</span><input v-model="form.ws_path" placeholder="/ray"></label>
        <label class="field" v-if="form.network==='ws'"><span>WS Host</span><input v-model="form.ws_host"></label>
        <label class="field"><span>TLS</span>
          <select v-model.number="form.tls"><option :value="0">关闭</option><option :value="1">开启</option></select>
        </label>
        <label class="field" v-if="form.tls"><span>SNI</span><input v-model="form.sni"></label>
      </div>
      <button class="btn primary" @click="doAdd" :disabled="loading">保存</button>
    </div>
  </div>

  <div class="card">
    <h2>节点列表（{{ nodes.length }}）</h2>
    <table v-if="nodes.length">
      <thead><tr>
        <th>名称</th><th>地址</th><th>传输</th><th>测速</th><th style="width:200px">操作</th>
      </tr></thead>
      <tbody>
        <tr v-for="n in nodes" :key="n.id" :class="{ active: proxy.alive && n.id === activeId }">
          <td>
            <span v-if="proxy.alive && n.id === activeId" class="tag ok" style="margin-right:6px">激活</span>
            <span v-else-if="n.id === activeId" class="tag off" style="margin-right:6px">上次使用</span>
            {{ n.name }}
          </td>
          <td class="mono">{{ n.address }}:{{ n.port }}</td>
          <td>{{ n.network }}{{ n.tls ? '+tls' : '' }}</td>
          <td>
            <template v-if="testing[n.id]">
              <span v-if="testing[n.id].loading" class="muted">测试中…</span>
              <span v-else-if="testing[n.id].ok" class="tag ok">{{ testing[n.id].latency_ms }}ms</span>
              <span v-else class="tag off" :title="testing[n.id].error">失败</span>
            </template>
            <span v-else class="muted">–</span>
          </td>
          <td class="row">
            <button class="btn" @click="doTest(n)">测速</button>
            <button class="btn primary" v-if="!(proxy.alive && n.id === activeId)" @click="doActivate(n)" :disabled="loading">启用</button>
            <button class="btn danger" @click="doDelete(n)">删</button>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-else class="muted">还没有节点。点右上角「导入 vmess://」或「手动添加」。</div>
  </div>
</template>

<style scoped>
.msg { margin-top: 10px; padding: 8px 12px; background: #e8f3ff; color: #1868f0; border-radius: 6px; font-size: 13px; }
.panel { margin-top: 14px; padding: 14px; background: #fafbfc; border: 1px solid #f0f1f3; border-radius: 8px; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 4px 14px; margin-bottom: 10px; }
.mono { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
tr.active { background: #f6fbff; }
</style>
