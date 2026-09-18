import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Served by the FastAPI bridge at /portal, built to hubzoid/portal_dist so the
// package ships a static SPA (no Node at runtime).
export default defineConfig({
  base: '/portal/',
  plugins: [react()],
  build: {
    outDir: '../hubzoid/portal_dist',
    emptyOutDir: true,
    // Screens are lazy-loaded (see Portal.tsx); the vendor split below keeps
    // the UI library in its own long-lived chunk. Ant Design itself is one
    // ~700 kB chunk that cannot be split further without dropping components,
    // so the warning threshold is raised to cover it rather than hide it.
    chunkSizeWarningLimit: 900,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: 'react', test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/, priority: 2 },
            { name: 'antd', test: /node_modules[\\/](antd|@ant-design|rc-[a-z-]+|@rc-component)[\\/]/, priority: 1 },
          ],
        },
      },
    },
  },
  server: { host: true, proxy: { '/portal/api': 'http://localhost:8000' } },
})
