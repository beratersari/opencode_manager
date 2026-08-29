import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8080',
      '/jobs': {
        target: 'http://127.0.0.1:8080',
        bypass(req) {
          if (req.method === 'GET') return req.url
        },
      },
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
