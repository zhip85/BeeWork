import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    proxy: {
      '/api': process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000',
      '/health': process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000',
      // The open API (/v1) and the website widget script/endpoints, so the docs and embed code work in dev.
      '/v1': process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000',
    },
  },
})
