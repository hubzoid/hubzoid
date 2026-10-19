// Development-only rendering for browser checks. No production bundle needed.
const path = require('node:path');
const fs = require('node:fs/promises');
async function attachDev(fixture) {
  const { createServer } = await import('vite');
  const root = path.resolve(__dirname, '..');
  const vite = await createServer({ root, base: '/', server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  const handler = fixture.server.listeners('request')[0];
  fixture.server.removeAllListeners('request');
  fixture.server.on('request', (req, res) => {
    if (/^\/(api|portal\/api|oauth|branding|artifacts|b\/)/.test(req.url)) return handler(req, res);
    vite.middlewares(req, res, async () => {
      try {
        const html = await vite.transformIndexHtml(req.url, await fs.readFile(path.join(root, 'index.html'), 'utf8'));
        res.writeHead(200, { 'content-type': 'text/html' });
        res.end(html);
      } catch (e) { res.writeHead(500); res.end(String(e)); }
    });
  });
  return vite;
}
module.exports = { attachDev };
