import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import { setupVConsole } from './vconsole'

// 按条件启用 vConsole（?debug=1 / localStorage / dev）
setupVConsole()

createApp(App).use(router).mount('#app')
