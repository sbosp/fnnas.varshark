import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 关键：base 必须等于网关前缀（带尾斜杠），否则 hash 资源 404 → 空窗
export default defineConfig({
  base: '/app/fnnas-rayshark/',
  plugins: [vue()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5273,
    proxy: {
      // 本地 dev：把 /api 转发到 Flask TCP 调试端口
      '/app/fnnas-rayshark/api': {
        target: 'http://127.0.0.1:8899',
        changeOrigin: true,
        rewrite: (p) => p.replace('/app/fnnas-rayshark', ''),
      },
    },
  },
})
