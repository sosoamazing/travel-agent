import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 前端开发服务器配置。后端 CORS 已放开，前端直接跨域访问 FastAPI（见 src/api.js）。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
})
