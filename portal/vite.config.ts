import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'

// Served by the FastAPI bridge at /portal, built to hubzoid/portal_dist so the
// package ships a static SPA (no Node at runtime).
export default defineConfig({
  base: '/portal/',
  plugins: [tailwindcss(), react()],
  build: { outDir: '../hubzoid/portal_dist', emptyOutDir: true },
  server: { host: true, proxy: { '/portal/api': 'http://localhost:8000' } },
})
