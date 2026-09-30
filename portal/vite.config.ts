import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'
import { readFileSync } from 'node:fs'

// One bundle for the Console (/portal/, hash routes) and the chat app (/, /c/*,
// /s/*, /auth*, /account*). main.tsx lazy-loads whichever one the path asks
// for, so neither pays for the other. Served by the FastAPI bridge and built to
// hubzoid/portal_dist so the package ships a static SPA (no Node at runtime).
// Tailwind only processes the chat app's stylesheet (src/app/app.css); the
// Console keeps its own CSS in portal.css.
const bridge = 'http://localhost:8000'

export default defineConfig({
  base: '/portal/',
  plugins: [react(), tailwindcss(), {
    name: 'bundled-font-licenses',
    generateBundle() {
      for (const name of ['Inter-OFL.txt', 'JetBrainsMono-OFL.txt']) {
        this.emitFile({ type: 'asset', fileName: `licenses/${name}`,
          source: readFileSync(new URL(`./src/assets/brand/fonts/${name}`, import.meta.url), 'utf8') })
      }
    },
  }],
  build: {
    outDir: '../hubzoid/portal_dist',
    emptyOutDir: true,
    // Screens are lazy-loaded (see Portal.tsx and app/App.tsx); the vendor
    // split below keeps the UI libraries in their own long-lived chunks. Ant
    // Design itself is one ~700 kB chunk that cannot be split further without
    // dropping components, so the warning threshold is raised to cover it
    // rather than hide it. The chat app never loads it.
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
  server: {
    host: true,
    proxy: Object.fromEntries(
      ['/portal/api', '/api', '/oauth', '/branding', '/artifacts', '/b/'].map((p) => [p, bridge]),
    ),
  },
})
