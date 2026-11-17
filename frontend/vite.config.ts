import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Frontend ходит только в наш backend; ключ Keitaro в браузер не попадает.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: { '/api': process.env.BACKEND_URL ?? 'http://127.0.0.1:8000' },
  },
})
