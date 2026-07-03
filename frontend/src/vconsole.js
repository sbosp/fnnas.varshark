// vConsole 集成：在 NAS iframe 内无法开浏览器 devtools 时用于调试。
//
// 开启条件（任一即可）：
//   1. URL 带 ?debug=1        （临时开，最常用）
//   2. localStorage.rayshark_debug = '1'  （持久开）
//   3. 开发模式 import.meta.env.DEV = true
//   4. URL 带 ?debug=0        （强制关，优先级最高）
//
// 面板里可看 console 日志、网络请求、WebSocket 消息、系统信息等，
// 对排查网关前缀、接口 4xx、ws 断连尤其有用。

let inst = null

function wanted() {
  const q = new URLSearchParams(location.search)
  if (q.get('debug') === '0') return false
  if (q.get('debug') === '1') {
    try { localStorage.setItem('rayshark_debug', '1') } catch {}
    return true
  }
  try {
    if (localStorage.getItem('rayshark_debug') === '1') return true
  } catch {}
  return !!import.meta.env.DEV
}

export async function setupVConsole() {
  if (!wanted() || inst) return
  try {
    const { default: VConsole } = await import('vconsole')
    inst = new VConsole({
      theme: 'light',
      defaultPlugins: ['system', 'network'],
    })
    // 暴露一个全局开关，方便运行时关掉
    window.__rayshark_vconsole = inst
    window.raysharkDebugOff = () => {
      try { localStorage.removeItem('rayshark_debug') } catch {}
      inst && inst.destroy()
      inst = null
    }
    // eslint-disable-next-line no-console
    console.log('[RayShark] vConsole 已启用。关闭：raysharkDebugOff() 或 URL 加 ?debug=0')
  } catch (e) {
    // vconsole 未安装或加载失败时静默
    // eslint-disable-next-line no-console
    console.warn('[RayShark] vConsole 加载失败', e)
  }
}

export function isDebug() {
  return !!inst
}
