#!/usr/bin/env node
// Serves the built portal (hubzoid/portal_dist) against the synthetic fixture
// API so the redesign can be inspected in a real browser without a bridge,
// Open WebUI, or any live data. Nothing here talks to a real deployment.
//
//   node tests/fixture-server.cjs [port]      (default 8791)
//
//   http://127.0.0.1:8791/portal/                 the portal (org admin by default)
//   http://127.0.0.1:8791/__fixture/role/org      switch viewer: org | hub | user
//   http://127.0.0.1:8791/__fixture/visibility/error   make the chat-app sync fail
//   http://127.0.0.1:8791/__fixture/reset         restore the initial data
//   http://127.0.0.1:8791/__fixture/state         mutations recorded so far (JSON)
//
// Auth: there is none — the fixture plays the role selected above. The real
// portal verifies the Open WebUI session cookie server-side.

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const { createFixture } = require("./fixture.cjs");

const port = Number(process.argv[2] || 8791);
const dist = path.resolve(__dirname, "../../hubzoid/portal_dist");
let fixture = createFixture();

const types = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml" };

function send(res, status, body, type = "application/json") {
  res.writeHead(status, { "content-type": type, "cache-control": "no-store" });
  res.end(type === "application/json" ? JSON.stringify(body) : body);
}

function placeholder(title, text) {
  return `<!doctype html><meta charset="utf-8"><title>${title}</title><body style="font:15px system-ui;padding:40px"><h1>${title}</h1><p>${text}</p><p><a href="/portal/">Back to the portal</a></p>`;
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  if (url.pathname.startsWith("/__fixture/")) {
    const [, , command, value] = url.pathname.split("/");
    if (command === "role" && ["org", "hub", "user"].includes(value)) fixture.state.role = value;
    else if (command === "visibility") fixture.state.visibility = value === "error" ? { state: "error", error: "ConnectError: visibility sync failed; check service credentials and server logs", updated: Math.floor(Date.now() / 1000) - 120 } : { state: value, updated: Math.floor(Date.now() / 1000) };
    else if (command === "reset") fixture = createFixture();
    else if (command === "state") return send(res, 200, { role: fixture.state.role, mutations: fixture.state.mutations, visibility: fixture.state.visibility });
    else return send(res, 404, { detail: "unknown fixture command" });
    return send(res, 200, { ok: true, role: fixture.state.role, visibility: fixture.state.visibility });
  }
  if (url.pathname.startsWith("/portal/api")) {
    const endpoint = url.pathname.slice("/portal/api".length);
    let body;
    if (req.method === "POST") {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {};
    }
    try {
      const data = await fixture.handle(req.method, endpoint, Object.fromEntries(url.searchParams), body);
      return send(res, 200, data);
    } catch (e) {
      return send(res, e.status || 500, { detail: e.detail || String(e) });
    }
  }
  if (url.pathname === "/" || url.pathname === "/admin")
    return send(res, 200, placeholder(url.pathname === "/" ? "Chat app (placeholder)" : "Open WebUI admin (placeholder)", "This stands in for Open WebUI in the fixture."), "text/html");
  if (url.pathname.startsWith("/portal")) {
    const relative = url.pathname.replace(/^\/portal\/?/, "") || "index.html";
    const file = path.join(dist, relative);
    if (file.startsWith(dist) && fs.existsSync(file) && fs.statSync(file).isFile())
      return send(res, 200, fs.readFileSync(file), types[path.extname(file)] || "application/octet-stream");
    return send(res, 200, fs.readFileSync(path.join(dist, "index.html")), "text/html");
  }
  send(res, 404, { detail: "not found" });
});

server.listen(port, "127.0.0.1", () => {
  console.log(`fixture portal: http://127.0.0.1:${port}/portal/  (role switch: /__fixture/role/org|hub|user)`);
});
