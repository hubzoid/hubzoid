// A stand-in for "your app's server": its own sign-in, the one route that gives
// the signed-in person a Hubzoid pass, and (for CopilotKit) the CopilotKit
// runtime that talks to Hubzoid. Nothing here is Hubzoid code: it uses jose to
// sign a JWT and CopilotKit's own runtime.
//
//   HUBZOID_URL=http://127.0.0.1:8600/b/finance \
//   HUBZOID_CLIENT_ID=erpnext HUBZOID_CLIENT_SECRET=... \
//   HUBZOID_AGENT=finance PORT=5180 node server.mjs
//
// Sign in as a demo person with /login?user=alice (your app's real sign-in
// takes this place). Pages: / (assistant-ui) and /copilotkit.html (CopilotKit).
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join } from "node:path";
import { SignJWT } from "jose";

const HUBZOID_URL = (process.env.HUBZOID_URL || "http://127.0.0.1:8600").replace(/\/$/, "");
const CLIENT_ID = process.env.HUBZOID_CLIENT_ID || "erpnext";
const SECRET = new TextEncoder().encode(process.env.HUBZOID_CLIENT_SECRET || "");
const AGENT = process.env.HUBZOID_AGENT || "finance";
const PORT = Number(process.env.PORT || 5180);
const DIST = join(import.meta.dirname, "dist");

// --- your app's own sign-in (a stand-in) -------------------------------------
function signedIn(req) {
  const m = /(?:^|;\s*)demo_user=([A-Za-z0-9_-]+)/.exec(req.headers.cookie || "");
  return m ? m[1] : null;
}

// --- the one Hubzoid-specific route: a pass for the signed-in person ---------
async function pass(user) {
  return new SignJWT({
    name: user,
    access: { agent: AGENT },               // what this person may use (within the app's limit)
    app_token: `demo-app-token-${user}`,    // the person's own token for your API
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuer(CLIENT_ID)
    .setAudience("hubzoid")
    .setSubject(user)
    .setIssuedAt()
    .setExpirationTime("10m")
    .sign(SECRET);
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
      const user = signedIn({ headers: { cookie: request.headers.get("cookie") || "" } });
      const headers = user ? { Authorization: `Bearer ${await pass(user)}` } : {};
      return { [AGENT]: new HttpAgent({ url: `${HUBZOID_URL}/api/agui`, headers }) };
    },
  });
  const handler = createCopilotRuntimeHandler({ runtime, basePath: "/api/copilotkit" });
  copilot = handler;
  return handler;
}

// CopilotKit's default runner keeps a thread's events in memory by thread id,
// for any caller. Thread ids are therefore tied to the signed-in person, and a
// request for someone else's thread is refused before it reaches the runtime.
function ownsThread(user, body) {
  const thread = body && (body.threadId || (body.body && body.body.threadId));
  return !thread || String(thread).startsWith(`${user}:`);
}

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };

createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    if (url.pathname === "/login") {
      const user = (url.searchParams.get("user") || "").replace(/[^A-Za-z0-9_-]/g, "");
      res.writeHead(302, { "Set-Cookie": `demo_user=${user}; Path=/; SameSite=Lax`, Location: url.searchParams.get("next") || "/" });
      return res.end();
    }
    const user = signedIn(req);
    if (url.pathname === "/hubzoid/pass") {
      if (!user) { res.writeHead(401); return res.end("Sign in first."); }
      res.writeHead(200, { "Content-Type": "text/plain", "Cache-Control": "no-store" });
      return res.end(await pass(user));
    }
    if (url.pathname === "/me") {
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ user, agent: AGENT, hubzoid: HUBZOID_URL }));
    }
    if (url.pathname.startsWith("/api/copilotkit")) {
      if (!user) { res.writeHead(401); return res.end("Sign in first."); }
      const chunks = [];
      for await (const c of req) chunks.push(c);
      const raw = Buffer.concat(chunks);
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
    res.writeHead(500); res.end("Error");
  }
}).listen(PORT, "127.0.0.1", () => console.log(`example app on http://127.0.0.1:${PORT}`));
