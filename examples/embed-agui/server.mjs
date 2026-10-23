// A stand-in for "your app": its own sign-in and its own data (an orders API
// and an MCP server that answer each person with their own orders), the one
// route that gives the signed-in person a Hubzoid pass, and (for CopilotKit)
// the CopilotKit runtime that talks to Hubzoid. Nothing here is Hubzoid code:
// it uses jose for JWTs, the MCP SDK and CopilotKit's own runtime.
//
//   HUBZOID_URL=http://127.0.0.1:8600/b/finance \
//   HUBZOID_CLIENT_ID=erpnext HUBZOID_CLIENT_SECRET=... \
//   HUBZOID_AGENT=finance PORT=5180 node server.mjs
//
// Mode 2 (your app's own JWT) also needs IDP_PRIVATE_KEY_FILE and
// IDP_PUBLIC_KEY_FILE: this server then stands in for your identity provider.
//
// Sign in as a demo person with /login?user=alice (your app's real sign-in
// takes this place); add &mode=jwt for mode 2. Pages: / (assistant-ui),
// /copilotkit.html (CopilotKit) and /source (what your API was asked, by whom).
import { createServer } from "node:http";
import { randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import { readFileSync } from "node:fs";
import { extname, join } from "node:path";
import { SignJWT, importPKCS8, importSPKI, jwtVerify } from "jose";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";

const HUBZOID_URL = (process.env.HUBZOID_URL || "http://127.0.0.1:8600").replace(/\/$/, "");
const CLIENT_ID = process.env.HUBZOID_CLIENT_ID || "erpnext";
const SECRET = new TextEncoder().encode(process.env.HUBZOID_CLIENT_SECRET || "");
const AGENT = process.env.HUBZOID_AGENT || "finance";
const PORT = Number(process.env.PORT || 5180);
const OTHER_SITE_PORT = Number(process.env.OTHER_SITE_PORT || 0);
const DIST = join(import.meta.dirname, "dist");
const IDP = {
  issuer: process.env.IDP_ISSUER || "https://idp.example.test/",
  audience: process.env.IDP_AUDIENCE || "hubzoid-e2e",
  privateKey: process.env.IDP_PRIVATE_KEY_FILE ? await importPKCS8(readFileSync(process.env.IDP_PRIVATE_KEY_FILE, "utf8"), "RS256") : null,
  publicKey: process.env.IDP_PUBLIC_KEY_FILE ? await importSPKI(readFileSync(process.env.IDP_PUBLIC_KEY_FILE, "utf8"), "RS256") : null,
};

// --- your app's own sign-in (a stand-in) -------------------------------------
// Each sign-in gets the app's own session token: your API accepts it, and the
// pass carries it to Hubzoid so the agent's tools call your API as this person.
const sessions = new Map();   // app token -> person
const tokenOf = new Map();    // person -> app token

function cookie(req, name) {
  const m = new RegExp(`(?:^|;\\s*)${name}=([^;]*)`).exec(req.headers.cookie || "");
  return m && m[1] ? decodeURIComponent(m[1]) : null;
}
const signedIn = (req) => cookie(req, "demo_user");

function appToken(user) {
  if (!tokenOf.has(user)) {
    const token = `acme_${randomBytes(18).toString("hex")}`;
    tokenOf.set(user, token);
    sessions.set(token, user);
  }
  return tokenOf.get(user);
}

// --- the one Hubzoid-specific route: a pass for the signed-in person ---------
// Mode 1: a pass signed with the client secret. Mode 2: your identity
// provider's own JWT, which Hubzoid checks with the provider's public key.
async function pass(req) {
  const user = signedIn(req);
  if (cookie(req, "demo_mode") === "jwt") {
    return new SignJWT({ name: user, scope: cookie(req, "demo_scope") || "openid chat:finance" })
      .setProtectedHeader({ alg: "RS256" })
      .setIssuer(IDP.issuer).setAudience(IDP.audience).setSubject(user)
      .setIssuedAt().setExpirationTime("5m")
      .sign(IDP.privateKey);
  }
  return new SignJWT({
    name: user,
    access: { agent: cookie(req, "demo_agent") || AGENT },  // what this person may use (within the app's limit)
    app_token: appToken(user),                                // the person's own token for your API
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuer(CLIENT_ID).setAudience("hubzoid").setSubject(user)
    .setIssuedAt().setExpirationTime("10m")
    .sign(SECRET);
}

// --- your app's data: each person sees only their own orders -----------------
const ORDERS = {
  alice: [
    { id: "SO-1001", customer: "Bright Tools", total: 1200, status: "Open" },
    { id: "SO-1002", customer: "Kite Farms", total: 640, status: "Open" },
  ],
  bob: [{ id: "SO-2001", customer: "Northwind Traders", total: 9800, status: "Open" }],
  dana: [{ id: "SO-3001", customer: "Lumen Labs", total: 450, status: "Shipped" }],
};
const calls = [];   // what your API was asked, for the /source page

// Who a token belongs to: your app's own session token, or (mode 2) your
// identity provider's JWT. Anything else is refused, as any API would.
async function personOf(authorization) {
  const token = (authorization || "").replace(/^Bearer\s+/i, "");
  if (sessions.has(token)) return { user: sessions.get(token), kind: "app session token" };
  if (IDP.publicKey && token.split(".").length === 3) {
    try {
      const { payload } = await jwtVerify(token, IDP.publicKey, { issuer: IDP.issuer });
      return { user: payload.sub, kind: "identity provider JWT" };
    } catch { /* not ours */ }
  }
  return { user: null, kind: token ? "unknown token" : "no token" };
}

async function orders(via, authorization, query) {
  const who = await personOf(authorization);
  const rows = who.user ? (ORDERS[who.user] || []) : null;
  calls.unshift({ at: new Date().toISOString().slice(11, 19), via, kind: who.kind, user: who.user,
                  status: rows ? 200 : 401, count: rows ? rows.length : 0, query: String(query || "") });
  calls.length = Math.min(calls.length, 50);
  return { who, rows };
}

function mcpServer(authorization) {
  const server = new McpServer({ name: "acme-erp", version: "1.0.0" });
  server.registerTool("list_orders", {
    description: "The signed-in person's open orders in Acme ERP.",
    inputSchema: { query: z.string().optional() },
  }, async ({ query }) => {
    const { who, rows } = await orders("MCP", authorization, query);
    const text = rows ? `Acme ERP orders for ${who.user}: ` + rows.map((r) => `${r.id} ${r.customer} ${r.total} (${r.status})`).join("; ")
      : "Acme ERP refused: that token is not a signed-in person.";
    return { content: [{ type: "text", text }], isError: !rows };
  });
  return server;
}

function sourcePage() {
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const rows = calls.map((c) => `<tr><td>${c.at}</td><td>${c.via}</td><td>${esc(c.kind)}</td><td>${esc(c.user || "—")}</td>`
    + `<td class="${c.status === 200 ? "ok" : "bad"}">${c.status}</td><td>${c.count}</td><td>${esc(c.query)}</td></tr>`).join("");
  return `<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="3"><title>Acme ERP API log</title>
<style>body{font:14px -apple-system,sans-serif;margin:24px;color:#1c1c1c}td,th{padding:6px 10px;border-bottom:1px solid #e4e4e1;text-align:left}
th{color:#6b6b6b;font-weight:500}.ok{color:#1f7a3a;font-weight:600}.bad{color:#b42318;font-weight:600}</style>
<h2 style="font-size:16px">Acme ERP API: calls received</h2><p style="color:#6b6b6b">Your app's own API, as the agent's tools reach it. Each call is answered for the person its token belongs to.</p>
<table><tr><th>Time (UTC)</th><th>Via</th><th>Token</th><th>Person</th><th>Status</th><th>Orders</th><th>Query</th></tr>${rows}</table>`;
}

// --- CopilotKit: its runtime, with Hubzoid registered as an AG-UI agent ------
let copilot = null;
async function copilotHandler() {
  if (copilot) return copilot;
  const { CopilotRuntime, createCopilotRuntimeHandler } = await import("@copilotkit/runtime/v2");
  const { HttpAgent } = await import("@ag-ui/client");
  const runtime = new CopilotRuntime({
    // A fresh pass on every request, for the person signed in to your app.
    agents: async ({ request }) => {
      const req = { headers: { cookie: request.headers.get("cookie") || "" } };
      const headers = signedIn(req) ? { Authorization: `Bearer ${await pass(req)}` } : {};
      return { [AGENT]: new HttpAgent({ url: `${HUBZOID_URL}/api/agui`, headers }) };
    },
  });
  copilot = createCopilotRuntimeHandler({ runtime, basePath: "/api/copilotkit" });
  return copilot;
}

// CopilotKit's default runner keeps a thread's events in memory by thread id,
// for any caller. Thread ids are therefore tied to the signed-in person, and a
// request for someone else's thread is refused before it reaches the runtime.
function ownsThread(user, body) {
  const thread = body && (body.threadId || (body.body && body.body.threadId));
  return !thread || String(thread).startsWith(`${user}:`);
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return Buffer.concat(chunks);
}

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };

createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    if (url.pathname === "/login") {
      const clean = (v) => (v || "").replace(/[^A-Za-z0-9_:. -]/g, "");
      const set = [["demo_user", clean(url.searchParams.get("user"))], ["demo_mode", clean(url.searchParams.get("mode"))],
                   ["demo_scope", clean(url.searchParams.get("scope"))], ["demo_agent", clean(url.searchParams.get("agent"))]];
      res.writeHead(302, { "Set-Cookie": set.map(([k, v]) => `${k}=${encodeURIComponent(v)}; Path=/; SameSite=Lax`),
                           Location: url.searchParams.get("next") || "/" });
      return res.end();
    }
    // Your app's API: the agent's hub tool calls it with the person's token.
    if (url.pathname === "/api/orders") {
      const { who, rows } = await orders("REST", req.headers.authorization, url.searchParams.get("q"));
      res.writeHead(rows ? 200 : 401, { "Content-Type": "application/json" });
      return res.end(JSON.stringify(rows ? { person: who.user, orders: rows } : { error: "not signed in" }));
    }
    // Your app's MCP server: added in Hubzoid as a connector, it gets the
    // person's token as a header on every call.
    if (url.pathname === "/mcp") {
      const raw = await readBody(req);
      const server = mcpServer(req.headers.authorization);
      const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
      res.on("close", () => { transport.close(); server.close(); });
      await server.connect(transport);
      return transport.handleRequest(req, res, raw.length ? JSON.parse(raw) : undefined);
    }
    if (url.pathname === "/source") {
      res.writeHead(200, { "Content-Type": "text/html" });
      return res.end(sourcePage());
    }
    const user = signedIn(req);
    if (url.pathname === "/hubzoid/pass") {
      if (!user) { res.writeHead(401); return res.end("Sign in first."); }
      res.writeHead(200, { "Content-Type": "text/plain", "Cache-Control": "no-store" });
      return res.end(await pass(req));
    }
    if (url.pathname === "/me") {
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ user, agent: AGENT, hubzoid: HUBZOID_URL,
                                      mode: cookie(req, "demo_mode") === "jwt" ? "jwt" : "pass" }));
    }
    if (url.pathname.startsWith("/api/copilotkit")) {
      if (!user) { res.writeHead(401); return res.end("Sign in first."); }
      const raw = await readBody(req);
      let body = null;
      try { body = raw.length ? JSON.parse(raw) : null; } catch { body = null; }
      if (!ownsThread(user, body)) { res.writeHead(403); return res.end("Not your chat."); }
      const handler = await copilotHandler();
      const request = new Request(url, { method: req.method, headers: req.headers, body: raw.length ? raw : undefined });
      const response = await handler(request);
      res.writeHead(response.status, Object.fromEntries(response.headers));
      if (response.body) for await (const chunk of response.body) res.write(chunk);
      return res.end();
    }
    const file = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
    const data = await readFile(join(DIST, file));
    res.writeHead(200, { "Content-Type": TYPES[extname(file)] || "application/octet-stream" });
    return res.end(data);
  } catch (err) {
    if (err && err.code === "ENOENT") { res.writeHead(404); return res.end("Not found"); }
    console.error(err);
    if (!res.headersSent) res.writeHead(500);
    res.end("Error");
  }
}).listen(PORT, "127.0.0.1", () => console.log(`example app on http://127.0.0.1:${PORT}`));

// A page on another site that got hold of a person's pass: the browser and
// Hubzoid refuse it, because the site is not one of the app's origins.
if (OTHER_SITE_PORT) {
  createServer(async (req, res) => {
    const copied = await pass({ headers: { cookie: "demo_user=alice" } });
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(`<!doctype html><meta charset="utf-8"><title>Another site</title>
<body style="font:14px -apple-system,sans-serif;margin:24px"><h2 style="font-size:16px">another-site.example (a page that is not the app)</h2>
<p>It holds a copy of alice's pass and tries to chat as her.</p><pre id="out">Trying…</pre>
<script>
fetch(${JSON.stringify(HUBZOID_URL + "/api/agui")}, {method: "POST", headers: {"Content-Type": "application/json", Authorization: "Bearer ${copied}"},
  body: JSON.stringify({threadId: "copied", runId: "r1", messages: [{id: "m1", role: "user", content: "list my orders"}], tools: [], context: []})})
  .then(r => r.text().then(t => document.getElementById("out").textContent = "Answered " + r.status + ": " + t.slice(0, 200)))
  .catch(e => document.getElementById("out").textContent = "Refused by the browser: " + e.message + " (this site is not one of the app's origins)");
</script>`);
  }).listen(OTHER_SITE_PORT, "127.0.0.1", () => console.log(`another site on http://127.0.0.1:${OTHER_SITE_PORT}`));
}
