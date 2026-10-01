// Browser journeys for the Hubzoid web app against REAL Hubzoid servers.
//
// Unlike app-journey.cjs (an in-memory fixture), this starts real bridges
// (`uvicorn hubzoid.server:build_app`) on fresh scratch hubs made with
// `hubzoid init`, using the scripted test model (MODEL=hubzoid-test/scripted
// with HUBZOID_TEST_RUNTIME=1: no model, no network, no cost):
//
//   accounts  sign-in on, the first administrator from HUBZOID_ADMIN_*
//   local     sign-in off (the local owner)
//   mcp       sign-in on, hosted MCP server on (MCP_SERVER=true): the remote
//             server a person connects to from the accounts hub. Browsed as
//             "localhost" so its session cookie never replaces the accounts
//             hub's (cookies ignore ports; 127.0.0.1 and localhost differ).
//
// Every server is stopped when the run ends, pass or fail.
//
//   npm run build && npm run test:app:real
//
// Settings (all optional):
//   HZ_PYTHON       a Python with Hubzoid's dependencies (default: python3)
//   APP_REAL_DIR    folder for the scratch hubs, server logs and screenshots
//                   (default: a new folder under the system temp folder)
//   APP_REAL_PORT   first port to try (default 3950); three free ports are used
//   APP_REAL_ONLY   comma-separated journey ids to run (default: all)
const { chromium, request: httpRequest } = require("playwright");
const { AxeBuilder } = require("@axe-core/playwright");
const assert = require("node:assert/strict");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const zlib = require("node:zlib");

const REPO = path.resolve(__dirname, "..", "..");
const PYTHON = process.env.HZ_PYTHON || "python3";
const WORK = path.resolve(process.env.APP_REAL_DIR || fs.mkdtempSync(path.join(os.tmpdir(), "hubzoid-app-real-")));
const SHOTS = path.join(WORK, "shots");
const ONLY = (process.env.APP_REAL_ONLY || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);
const ID = /^[A-Za-z0-9_-]{8,64}$/;
const SLOW_SECONDS = 12; // the scripted "slow" reply: 40 chunks over this long

const ADMIN = { email: "ada@example.com", password: "ada-admin-pass-1", name: "Ada Lovelace" };
const SAM = { email: "sam@example.com", password: "sam-member-pass-1", name: "Sam Rivera" };
const MCP_OWNER = { email: "owner@example.com", password: "mcp-owner-pass-1", name: "Mo Owner" };
const AGENT_NAME = "Acme Help Desk";

fs.mkdirSync(SHOTS, { recursive: true });

// ---- servers ------------------------------------------------------------------------

function portFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.listen({ port, host: "127.0.0.1", exclusive: true }, () => server.close(() => resolve(true)));
  });
}

async function freePorts(count, start) {
  const ports = [];
  for (let port = start; ports.length < count && port < start + 200; port++) if (await portFree(port)) ports.push(port);
  if (ports.length < count) throw new Error(`no ${count} free ports from ${start}`);
  return ports;
}

function python(args, options = {}) {
  const result = spawnSync(PYTHON, args, {
    cwd: REPO,
    env: { ...process.env, PYTHONPATH: REPO, ...(options.env || {}) },
    encoding: "utf8",
    timeout: 300_000,
  });
  if (result.status !== 0)
    throw new Error(`${PYTHON} ${args.join(" ")} failed (${result.status}):\n${result.stdout}\n${result.stderr}`);
  return result.stdout;
}

/** A fresh hub from `hubzoid init`, answering with the scripted test model. */
function makeHub(name, frontmatter) {
  const dir = path.join(WORK, name, "hub");
  fs.rmSync(path.join(WORK, name), { recursive: true, force: true });
  // The minimal template: its frontmatter has no name, so the journeys' own
  // agent names apply (the default template names its own fictional agent).
  python(["-m", "hubzoid.cli", "init", dir, "--model", "claude-local", "--template", "minimal"]);
  const env = path.join(dir, ".env");
  const text = fs.readFileSync(env, "utf8");
  assert.match(text, /^MODEL=claude-local$/m, "hubzoid init writes MODEL=claude-local");
  fs.writeFileSync(env, text.replace(/^MODEL=claude-local$/m, "MODEL=hubzoid-test/scripted"));
  if (frontmatter) {
    const agents = path.join(dir, "AGENTS.md");
    fs.writeFileSync(agents, fs.readFileSync(agents, "utf8").replace(/^---\n/, `---\n${frontmatter}\n`));
  }
  return dir;
}

class Server {
  constructor(name, hub, port, host, env) {
    Object.assign(this, { name, hub, port, env });
    this.base = `http://${host}:${port}`;
    this.log = path.join(WORK, name, "server.log");
    this.proc = null;
  }

  start() {
    const out = fs.openSync(this.log, "w");
    this.proc = spawn(
      PYTHON,
      ["-m", "uvicorn", "hubzoid.server:build_app", "--factory", "--host", "127.0.0.1", "--port", String(this.port)],
      {
        cwd: REPO,
        detached: true, // its own process group, so stop() reaches every child
        stdio: ["ignore", out, out],
        env: {
          ...process.env,
          PYTHONPATH: REPO,
          HUBZOID_HUB_DIR: this.hub,
          HUBZOID_TEST_RUNTIME: "1",
          HUBZOID_TEST_SLOW_SECONDS: String(SLOW_SECONDS),
          HUBZOID_SCHEDULES: "false",
          // As `hubzoid run --no-ui` tells its bridge: the port it serves on.
          BRIDGE_PORT: String(this.port),
          ...this.env,
        },
      },
    );
    this.proc.on("exit", (code, signal) => {
      this.exited = { code, signal };
    });
  }

  async ready(timeoutMs = 240_000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (this.exited) throw new Error(`${this.name} server exited (${JSON.stringify(this.exited)}); see ${this.log}`);
      try {
        const res = await fetch(`http://127.0.0.1:${this.port}/api/auth/session`);
        if (res.ok) return;
      } catch {
        /* not listening yet */
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    throw new Error(`${this.name} server did not start in time; see ${this.log}`);
  }

  async stop() {
    if (!this.proc || this.exited) return;
    const group = -this.proc.pid;
    try {
      process.kill(group, "SIGTERM");
    } catch {
      return;
    }
    const deadline = Date.now() + 15_000;
    while (!this.exited && Date.now() < deadline) await new Promise((r) => setTimeout(r, 200));
    if (!this.exited) {
      try {
        process.kill(group, "SIGKILL");
      } catch {
        /* already gone */
      }
    }
  }
}

// ---- helpers ------------------------------------------------------------------------

const steps = [];
const results = [];
function step(name) {
  steps.push(name);
  console.log(`  · ${name}`);
}

/** A small bar chart as a PNG, so image uploads have something to show. */
function chartPng(width = 160, height = 100) {
  const crcTable = Array.from({ length: 256 }, (_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  const crc32 = (buf) => {
    let c = 0xffffffff;
    for (const b of buf) c = crcTable[(c ^ b) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (type, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type), data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(body));
    return Buffer.concat([len, body, crc]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 2;
  const bars = [0.45, 0.7, 0.55, 0.9];
  const rows = [];
  for (let y = 0; y < height; y++) {
    const row = Buffer.alloc(1 + width * 3);
    for (let x = 0; x < width; x++) {
      const bar = Math.floor((x - 16) / 34);
      const inBar = bar >= 0 && bar < 4 && (x - 16) % 34 < 24 && y > height - 12 - bars[bar] * (height - 24) && y < height - 12;
      const [r, g, b] = inBar ? [0xe5, 0x57, 0x2a] : y === height - 12 ? [0xcf, 0xcd, 0xc7] : [0xfa, 0xfa, 0xf8];
      row[1 + x * 3] = r;
      row[2 + x * 3] = g;
      row[3 + x * 3] = b;
    }
    rows.push(row);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", zlib.deflateSync(Buffer.concat(rows))),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

// ---- the run --------------------------------------------------------------------------

async function main() {
  const servers = [];
  let browser = null;
  const errors = [];
  let diagnosticPage = null;
  try {
    console.log(`Scratch folder: ${WORK}`);
    const [accountsPort, localPort, mcpPort] = await freePorts(3, Number(process.env.APP_REAL_PORT || 3950));

    console.log("Creating scratch hubs…");
    const accountsHub = makeHub("accounts", `name: ${AGENT_NAME}`);
    const localHub = makeHub("local", "name: Local Helper");
    const mcpHub = makeHub("mcp", "name: Remote Notes");

    const accounts = new Server("accounts", accountsHub, accountsPort, "127.0.0.1", {
      HUBZOID_AUTH: "true",
      HUBZOID_ADMIN_EMAIL: ADMIN.email,
      HUBZOID_ADMIN_PASSWORD: ADMIN.password,
      HUBZOID_ADMIN_NAME: ADMIN.name,
      // Reasoning text in full, so the reasoning panel has something to show.
      SHOW_THINKING: "full",
    });
    const local = new Server("local", localHub, localPort, "127.0.0.1", {});
    const mcp = new Server("mcp", mcpHub, mcpPort, "localhost", {
      HUBZOID_AUTH: "true",
      HUBZOID_ADMIN_EMAIL: MCP_OWNER.email,
      HUBZOID_ADMIN_PASSWORD: MCP_OWNER.password,
      HUBZOID_ADMIN_NAME: MCP_OWNER.name,
      HUBZOID_PUBLIC_URL: `http://localhost:${mcpPort}`,
      MCP_SERVER: "true",
      MCP_PUBLIC_URL: `http://localhost:${mcpPort}/mcp`,
    });
    servers.push(accounts, local, mcp);
    console.log(`Starting servers: accounts ${accounts.base}, local ${local.base}, mcp ${mcp.base}`);
    for (const s of servers) s.start();
    await Promise.all(servers.map((s) => s.ready()));
    console.log("Servers are up.");

    const BASE = accounts.base;
    browser = await chromium.launch({ headless: true });

    const watch = (page) => {
      page.on("pageerror", (e) => errors.push(`${page.url()} pageerror: ${e.message}`));
      page.on("console", (m) => {
        // Non-2xx fetches log "Failed to load resource"; the negative journeys
        // provoke 401/403/404 on purpose and the app handles them in the UI.
        if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(`${page.url()}: ${m.text()}`);
      });
      return page;
    };
    const shot = async (page, name) => {
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
    };
    const axe = async (page, label) => {
      const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      const bad = result.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
      assert.deepEqual(
        bad.map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(" | ")})`),
        [],
        `${label}: serious or critical accessibility violations`,
      );
    };
    const noHorizontalScroll = async (page, label) => {
      const [scroll, width] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
      assert.ok(scroll <= width, `${label}: page scrolls sideways (${scroll} > ${width})`);
    };

    /** A browser context (its own cookies) with a page on it. */
    const open = async (options = {}) => {
      const context = await browser.newContext({ viewport: { width: 1360, height: 900 }, ...options });
      const page = watch(await context.newPage());
      return { context, page };
    };
    const signIn = async (page, who, base = BASE) => {
      await page.getByLabel("Email").fill(who.email);
      await page.getByLabel("Password").fill(who.password);
      await page.getByRole("button", { name: "Sign in", exact: true }).click();
    };
    const signedIn = async (who, path = "/") => {
      const session = await open();
      await session.page.goto(`${BASE}${path}`);
      await session.page.waitForURL(/\/auth/);
      await signIn(session.page, who);
      await session.page.waitForURL((url) => !url.pathname.startsWith("/auth"));
      return session;
    };

    // Chat helpers, bound to one page.
    const chat = (page) => {
      const composer = () => page.getByRole("textbox", { name: /^Message / });
      return {
        composer,
        send: async (text) => {
          await composer().fill(text);
          await composer().press("Enter");
        },
        lastAssistant: () => page.locator('[data-role="assistant"]').last(),
        idle: () => page.getByTestId("send").waitFor({ timeout: 60_000 }),
        settled: async () => {
          await composer().waitFor();
          await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label")?.startsWith("Message "));
        },
        conversationId: () => decodeURIComponent(new URL(page.url()).pathname.split("/c/")[1] || ""),
        sidebar: () => page.getByRole("navigation", { name: "Conversations" }),
      };
    };

    // Server-side reads for assertions, as the person signed in on `context`.
    const apiGet = async (context, url) => {
      const res = await context.request.get(url.startsWith("http") ? url : `${BASE}${url}`);
      return { status: res.status(), json: res.ok() ? await res.json().catch(() => null) : null, res };
    };

    // ---- accounts the journeys need -----------------------------------------------
    // Sam: an ordinary member with use_hub on the accounts hub, made through the
    // real Console API (Add user returns a one-time link) and the link API.
    const setup = await httpRequest.newContext({ baseURL: BASE, extraHTTPHeaders: { Origin: BASE } });
    {
      const login = await setup.post("/api/auth/login", { data: { email: ADMIN.email, password: ADMIN.password } });
      assert.equal(login.status(), 200, `admin sign-in: ${await login.text()}`);
      const hubs = await (await setup.get("/portal/api/hubs")).json();
      const hubKey = hubs.hubs[0].key;
      const created = await setup.post("/portal/api/accounts", {
        data: { email: SAM.email, name: SAM.name, sign_in: "password", grants: [{ hub: hubKey, permission: "use_hub" }] },
      });
      const body = await created.json();
      assert.ok(created.ok() && body.link, `Add user for Sam: ${created.status()} ${JSON.stringify(body)}`);
      const token = new URL(body.link, BASE).searchParams.get("token");
      const anon = await httpRequest.newContext({ baseURL: BASE, extraHTTPHeaders: { Origin: BASE } });
      const set = await anon.post(`/api/auth/link/${encodeURIComponent(token)}`, { data: { password: SAM.password } });
      assert.equal(set.status(), 200, `Sam sets a password: ${await set.text()}`);
      await anon.dispose();
    }

    const ctx = {
      BASE,
      accounts,
      local,
      mcp,
      setup,
      browser,
      open,
      signIn,
      signedIn,
      chat,
      apiGet,
      shot,
      axe,
      noHorizontalScroll,
      watch,
      errors,
      setDiagnostic: (page) => {
        diagnosticPage = page;
      },
    };

    for (const [id, title, fn] of JOURNEYS) {
      if (ONLY.length && !ONLY.includes(id)) continue;
      console.log(`\n[${id}] ${title}`);
      const started = Date.now();
      await fn(ctx);
      results.push({ id, title, seconds: Math.round((Date.now() - started) / 100) / 10 });
    }

    assert.deepEqual(errors, [], "no console or page errors");
    console.log(`\nPASS: ${results.length} journeys, ${steps.length} checks. Screenshots: ${SHOTS}`);
    for (const r of results) console.log(`  ${r.id.padEnd(12)} ${String(r.seconds).padStart(5)} s  ${r.title}`);
  } catch (error) {
    if (diagnosticPage) {
      fs.writeFileSync(path.join(SHOTS, "failure.html"), await diagnosticPage.content().catch(() => ""));
      await diagnosticPage.screenshot({ path: path.join(SHOTS, "failure.png") }).catch(() => {});
    }
    // Every page still open, for the journeys that use several people at once.
    let n = 0;
    for (const context of browser?.contexts() ?? [])
      for (const p of context.pages()) {
        n++;
        console.error(`  open page ${n}: ${p.url()}`);
        await p.screenshot({ path: path.join(SHOTS, `failure-page-${n}.png`) }).catch(() => {});
      }
    if (errors.length) console.error("console errors:", errors);
    console.error(`\nFAIL at: ${steps[steps.length - 1]}\n`, error);
    for (const s of servers) console.error(`  ${s.name} log: ${s.log}`);
    process.exitCode = 1;
  } finally {
    await browser?.close().catch(() => {});
    await Promise.all(servers.map((s) => s.stop()));
  }
}

// ---- journeys ---------------------------------------------------------------------------
// Each is [id, title, async (ctx) => {}]. They share the servers and the two
// accounts (the administrator Ada and the member Sam), and each makes its own chats.

const JOURNEYS = [];
const journey = (id, title, fn) => JOURNEYS.push([id, title, fn]);

journey("signin", "Sign-in: wrong password, return to the requested page", async (ctx) => {
  const { page, context } = await ctx.open();
  ctx.setDiagnostic(page);
  step("A signed-out visitor goes to sign-in, keeping the page to come back to");
  await page.goto(`${ctx.BASE}/account/connections`);
  await page.waitForURL(/\/auth\?redirect=%2Faccount%2Fconnections$/);
  await page.getByRole("heading", { name: `Sign in to ${AGENT_NAME}` }).waitFor();
  await ctx.shot(page, "01-sign-in");
  await ctx.axe(page, "sign-in");

  step("A wrong password reads as a plain sentence");
  await ctx.signIn(page, { email: ADMIN.email, password: "not-the-password" });
  await page.getByText("That email and password don't match. Check them and try again.").waitFor();
  await ctx.shot(page, "02-sign-in-wrong-password");

  step("Signing in returns to the page that asked for it");
  await ctx.signIn(page, ADMIN);
  await page.waitForURL(`${ctx.BASE}/account/connections`);
  await page.getByRole("heading", { name: "Connections", level: 1 }).waitFor();
  await context.close();
});


/** Every state a tool entry shows, in order, for entries of `toolName`. */
async function watchToolStates(page) {
  await page.evaluate(() => {
    window.__toolStates = [];
    const seen = (el) => {
      const name = el.querySelector("button .font-mono")?.textContent || "";
      const state = el.getAttribute("data-tool-state");
      const list = window.__toolStates;
      const last = list.filter((x) => x.el === el).pop();
      if (!last || last.state !== state) list.push({ el, name, state });
    };
    document.querySelectorAll("[data-tool-state]").forEach(seen);
    new MutationObserver((records) => {
      for (const r of records) {
        if (r.type === "attributes" && r.target instanceof Element && r.target.hasAttribute("data-tool-state")) seen(r.target);
        for (const n of r.addedNodes || [])
          if (n instanceof Element) {
            if (n.hasAttribute("data-tool-state")) seen(n);
            n.querySelectorAll?.("[data-tool-state]").forEach(seen);
          }
      }
    }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["data-tool-state"] });
  });
  return async (toolName) =>
    page.evaluate((name) => window.__toolStates.filter((x) => x.name === name).map((x) => x.state), toolName);
}

journey("chat", "New chat: suggestions, streaming markdown, a URL and a generated title", async (ctx) => {
  const { page, context } = await ctx.signedIn(ADMIN);
  ctx.setDiagnostic(page);
  const c = ctx.chat(page);
  const agents = (await ctx.apiGet(context, "/api/agents")).json;
  const agentId = agents.agents[0].id;

  step("The new chat page greets with the agent's name and offers its suggestions");
  await page.getByRole("heading", { name: `What can ${AGENT_NAME} help with?` }).waitFor();
  await page.getByRole("button", { name: "What is in this hub?" }).waitFor();
  await c.settled();
  // One hub: the agent's name is also the brand, and a title never repeats it.
  assert.equal(await page.title(), `New chat · ${AGENT_NAME}`);
  await ctx.shot(page, "03-new-chat");
  await ctx.axe(page, "new chat");

  step("?agent= (and the Console's ?models=) preselect the agent; an unknown one says so");
  await page.goto(`${ctx.BASE}/?agent=${encodeURIComponent(agentId)}`);
  await page.getByRole("heading", { name: `What can ${AGENT_NAME} help with?` }).waitFor();
  await page.goto(`${ctx.BASE}/?models=${encodeURIComponent(agentId)}`);
  await page.getByRole("heading", { name: `What can ${AGENT_NAME} help with?` }).waitFor();
  await page.goto(`${ctx.BASE}/?agent=no-such-agent`);
  await page.getByRole("heading", { name: `What can ${AGENT_NAME} help with?` }).waitFor();
  await ctx.shot(page, "04-new-chat-unknown-agent");

  step("A suggestion sends at once; the reply streams in and the chat gets its own URL");
  await page.goto(`${ctx.BASE}/`);
  await c.settled();
  await page.getByRole("button", { name: "What is in this hub?" }).click();
  await page.waitForURL(/\/c\/[^/]+$/);
  await c.idle();
  const id = c.conversationId();
  assert.match(id, ID);
  await c.lastAssistant().getByText("You said: “What is in this hub?”").waitFor();
  await c.lastAssistant().getByText("This is a scripted reply from the Hubzoid test runtime.").waitFor();
  await page.getByTestId("user-text").getByText("What is in this hub?").waitFor();

  step("The generated title replaces the first words, in the sidebar and the header");
  await c.sidebar().getByRole("link", { name: "About what is in this" }).waitFor({ timeout: 20_000 });
  await page.getByTestId("chat-title").getByText("About what is in this").waitFor();
  await page.getByTestId("live-region").getByText("Response complete").waitFor();
  await page.waitForFunction((t) => document.title === t, `About what is in this · ${AGENT_NAME}`);

  step("A markdown reply: a table, highlighted code and a working copy button");
  await c.send("Please answer in markdown");
  await c.lastAssistant().getByRole("table").waitFor();
  await c.idle();
  await c.lastAssistant().getByRole("columnheader", { name: "Item" }).waitFor();
  await c.lastAssistant().getByRole("cell", { name: "Pears" }).waitFor();
  await c.lastAssistant().locator(".shiki").first().waitFor();
  await c.lastAssistant().getByText("And a code sample:").waitFor();
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: ctx.BASE });
  await c.lastAssistant().getByRole("button", { name: "Copy code" }).click();
  await c.lastAssistant().getByRole("button", { name: "Copied" }).waitFor();
  assert.match(await page.evaluate(() => navigator.clipboard.readText()), /def total\(counts\):\n {4}return sum\(counts\)/);
  await ctx.shot(page, "05-markdown-reply");
  await ctx.axe(page, "conversation");

  step("After a reload the conversation reads the same, with its title");
  await page.reload();
  await c.composer().waitFor();
  await c.lastAssistant().getByRole("table").waitFor();
  await page.getByTestId("chat-title").getByText("About what is in this").waitFor();
  assert.equal(await page.locator('[data-role="user"]').count(), 2);
  await context.close();
});

journey("tools", "Tool entries (running, done, failed) and the reasoning panel", async (ctx) => {
  const { page, context } = await ctx.signedIn(ADMIN);
  ctx.setDiagnostic(page);
  const c = ctx.chat(page);
  await c.settled();
  const states = await watchToolStates(page);

  step("A tool call shows Running, then Done, as a collapsed entry with its input");
  await c.send("Please use a tool to check the pricing");
  await c.idle();
  const entry = page.getByTestId("tool-entry").filter({ hasText: "read_knowledge" });
  await entry.getByText("Done").waitFor();
  assert.deepEqual(await states("read_knowledge"), ["running", "done"]);
  const toggle = entry.getByRole("button", { name: /read_knowledge/ });
  assert.equal(await toggle.getAttribute("aria-expanded"), "false");
  await toggle.click();
  assert.equal(await toggle.getAttribute("aria-expanded"), "true");
  await entry.getByText('"name": "scripted"').waitFor();

  step("A failing tool shows Failed and a plain sentence, not a stack trace");
  await c.send("Now try the call that will fail");
  await c.idle();
  const failed = page.getByTestId("tool-entry").filter({ hasText: "grep_data" });
  await failed.getByText("Failed").waitFor();
  // The sentence shows under the collapsed entry (and again inside it when opened).
  await failed.getByText("The tool did not complete. The agent may retry or ask for more information.").first().waitFor();
  assert.deepEqual(await states("grep_data"), ["running", "failed"]);
  await ctx.shot(page, "06-tools");

  step("Reasoning is a collapsed Thought process panel that opens to the text");
  await c.send("Think it through before you answer");
  await c.idle();
  const thought = page.getByRole("button", { name: /Thought process/ }).last();
  assert.equal(await thought.getAttribute("aria-expanded"), "false");
  await thought.click();
  assert.equal(await thought.getAttribute("aria-expanded"), "true");
  await page.getByText("Considering the request step by step.").waitFor();
  await ctx.shot(page, "07-reasoning");

  step("After a reload, tool entries and reasoning come back from the stored message");
  await page.reload();
  await c.composer().waitFor();
  await page.getByTestId("tool-entry").filter({ hasText: "read_knowledge" }).getByText("Done").waitFor();
  await page.getByTestId("tool-entry").filter({ hasText: "grep_data" }).getByText("Failed").waitFor();
  await page.getByRole("button", { name: /Thought process/ }).waitFor();
  await context.close();
});

journey("stop", "Stop a slow reply: the stream ends, the partial text stays, cancelled after reload", async (ctx) => {
  const { page, context } = await ctx.signedIn(ADMIN);
  ctx.setDiagnostic(page);
  const c = ctx.chat(page);
  await c.settled();
  step("Stop ends a slow reply where it is");
  await c.send("Give me a slow answer please");
  await c.lastAssistant().getByText("Chunk 3.", { exact: false }).waitFor();
  await page.getByRole("button", { name: "Stop response" }).click();
  await c.lastAssistant().getByTestId("stopped").waitFor();
  await c.idle();
  await page.getByTestId("live-region").getByText("Response stopped").waitFor();
  const id = c.conversationId();
  const shown = await c.lastAssistant().innerText();
  assert.ok(!shown.includes("Chunk 40."), "the reply stopped before its end");
  await page.waitForTimeout(1500);
  const detail = (await ctx.apiGet(context, `/api/conversations/${id}`)).json;
  const reply = detail.messages.find((m) => m.role === "assistant");
  assert.equal(reply.status, "cancelled");
  const stored = reply.content.filter((p) => p.type === "text").map((p) => p.text).join("");
  assert.match(stored, /^Chunk 1\. Chunk 2\. Chunk 3\./);
  assert.ok(!stored.includes("Chunk 40."));
  await ctx.shot(page, "08-stopped");

  step("After a reload the partial reply is still there, marked Stopped");
  await page.reload();
  await c.composer().waitFor();
  await c.lastAssistant().getByTestId("stopped").waitFor();
  await c.lastAssistant().getByText("Chunk 3.", { exact: false }).waitFor();
  assert.equal(await page.getByRole("button", { name: "Stop response" }).count(), 0);

  step("The next message goes through after a stopped one");
  await c.send("Thanks, that is enough");
  await c.idle();
  await c.lastAssistant().getByText("You said: “Thanks, that is enough”").waitFor();
  await context.close();
});

journey("reload", "Reload while a reply is running: progress shows and it completes by itself", async (ctx) => {
  const { page, context } = await ctx.signedIn(ADMIN);
  ctx.setDiagnostic(page);
  const c = ctx.chat(page);
  await c.settled();
  step("A slow reply is running when the page reloads");
  await c.send("Another slow answer, please");
  await c.lastAssistant().getByText("Chunk 2.", { exact: false }).waitFor();
  await page.waitForURL(/\/c\//);
  const id = c.conversationId();
  await page.reload();
  step("The reloaded page shows the reply in progress, with Stop");
  await page.getByRole("button", { name: "Stop response" }).waitFor();
  await c.lastAssistant().getByText(/Chunk (1[0-9]|[5-9])\./).waitFor({ timeout: 20_000 });
  await ctx.shot(page, "09-reload-running");
  step("It finishes on its own, and the final text is the whole reply");
  await c.lastAssistant().getByText("Chunk 40.", { exact: false }).waitFor({ timeout: 30_000 });
  await c.idle();
  assert.equal(await page.getByTestId("streaming").count(), 0);
  const detail = (await ctx.apiGet(context, `/api/conversations/${id}`)).json;
  assert.equal(detail.messages.find((m) => m.role === "assistant").status, "complete");
  await page.reload();
  await c.lastAssistant().getByText("Chunk 40.", { exact: false }).waitFor();
  await context.close();
});

journey("branches", "Edit and regenerate make branches; the picker switches; the choice survives a reload", async (ctx) => {
  const { page, context } = await ctx.signedIn(ADMIN);
  ctx.setDiagnostic(page);
  const c = ctx.chat(page);
  await c.settled();
  step("Edit a sent message: a second version with its own reply");
  await c.send("Tell me about hubs");
  await page.waitForURL(/\/c\//);
  await c.idle();
  const id = c.conversationId();
  const userMsg = page.getByRole("article", { name: "You said" }).first();
  await userMsg.hover();
  await userMsg.getByRole("button", { name: "Edit message" }).click();
  const edit = page.getByRole("textbox", { name: "Edit message" });
  await edit.fill("Tell me about agents instead");
  await ctx.shot(page, "10-editing");
  await edit.press("Enter");
  await c.lastAssistant().getByText("You said: “Tell me about agents instead”").waitFor();
  await c.idle();
  assert.deepEqual(await page.getByTestId("branch-count").allTextContents(), ["2 / 2"]);

  step("Regenerate the reply: a second reply version");
  await c.lastAssistant().hover();
  await c.lastAssistant().getByRole("button", { name: "Regenerate response" }).click();
  await c.idle();
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="branch-count"]').length === 2);
  assert.deepEqual(await page.getByTestId("branch-count").allTextContents(), ["2 / 2", "2 / 2"]);
  let detail = (await ctx.apiGet(context, `/api/conversations/${id}`)).json;
  assert.equal(detail.messages.filter((m) => m.role === "user").length, 2);
  assert.equal(detail.messages.filter((m) => m.role === "assistant").length, 3);

  step("The branch picker goes back to the first version");
  await page.getByRole("button", { name: "Previous version" }).first().click();
  await page.getByTestId("user-text").getByText("Tell me about hubs").waitFor();
  assert.deepEqual(await page.getByTestId("branch-count").allTextContents(), ["1 / 2"]);
  await c.lastAssistant().getByText("You said: “Tell me about hubs”").waitFor();
  await ctx.shot(page, "11-branches");

  step("After a reload the chosen branch is still the one on screen");
  const firstReply = detail.messages.find((m) => m.role === "assistant" && m.parent_id === detail.messages.find((u) => u.role === "user" && u.content.some((p) => p.text === "Tell me about hubs")).id);
  await page.waitForFunction(
    async ([url, head]) => (await (await fetch(url)).json()).head_id === head,
    [`/api/conversations/${id}`, firstReply.id],
  );
  await page.reload();
  await page.getByTestId("user-text").getByText("Tell me about hubs").waitFor();
  assert.deepEqual(await page.getByTestId("branch-count").allTextContents(), ["1 / 2"]);
  await c.lastAssistant().getByText("You said: “Tell me about hubs”").waitFor();

  step("The other branch and both of its reply versions are still reachable after the reload");
  await page.getByRole("button", { name: "Next version" }).first().click();
  await page.getByTestId("user-text").getByText("Tell me about agents instead").waitFor();
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="branch-count"]').length === 2);
  const [userCount, replyCount] = await page.getByTestId("branch-count").allTextContents();
  assert.equal(userCount, "2 / 2");
  // Off the remembered branch, assistant-ui opens a message's first reply version.
  if (replyCount === "1 / 2") await page.getByRole("button", { name: "Next version" }).last().click();
  await page.waitForFunction(() => [...document.querySelectorAll('[data-testid="branch-count"]')].map((e) => e.textContent).join() === "2 / 2,2 / 2");
  await c.lastAssistant().getByText("You said: “Tell me about agents instead”").waitFor();
  await context.close();
});

journey("uploads", "A text file and an image: on the message, in the reply, downloadable by the owner only", async (ctx) => {
  const { page, context } = await ctx.signedIn(ADMIN);
  ctx.setDiagnostic(page);
  const c = ctx.chat(page);
  await c.settled();
  step("Both files upload right away and show in the composer");
  const chooser = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: "Attach files" }).click();
  await (await chooser).setFiles([
    { name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("Quarterly notes: revenue up 4 percent.\n") },
    { name: "chart.png", mimeType: "image/png", buffer: chartPng() },
  ]);
  const chips = page.getByTestId("composer-attachment");
  await page.waitForFunction(
    () =>
      document.querySelectorAll('[data-testid="composer-attachment"]').length === 2 &&
      [...document.querySelectorAll('[data-testid="composer-attachment"]')].every((el) => el.dataset.status === "requires-action"),
  );
  assert.equal(await chips.count(), 2);
  await page.getByTestId("composer").getByRole("img", { name: "Preview of chart.png" }).waitFor();
  await ctx.shot(page, "12-attachments-ready");

  step("They go with the message, and the reply names both");
  await c.send("What do these files show?");
  await page.waitForURL(/\/c\//);
  await c.idle();
  const id = c.conversationId();
  const sent = page.getByRole("article", { name: "You said" }).last();
  await sent.getByRole("link", { name: "Preview of chart.png" }).waitFor();
  await sent.getByText("notes.txt").waitFor();
  // The scripted model lists the files named in the attachment notes of its prompt.
  await c.lastAssistant().getByText("Attached: notes.txt, chart.png.").waitFor();
  await ctx.shot(page, "13-attachments-sent");

  step("After a reload the files are still on the message, served by the server");
  await page.reload();
  const stored = page.getByRole("article", { name: "You said" }).last();
  await stored.getByRole("link", { name: "Preview of chart.png" }).waitFor();
  await page.waitForFunction(() => document.querySelector('img[alt="chart.png"]')?.complete);
  assert.equal(await page.locator('img[alt="chart.png"]').evaluate((img) => img.naturalWidth), 160);
  await ctx.shot(page, "13b-attachments-after-reload");

  step("The owner can open both files; the image is a real PNG");
  const fileLink = stored.getByRole("link").filter({ hasText: "notes.txt" });
  const fileHref = await fileLink.getAttribute("href");
  const imageHref = await stored.getByRole("link", { name: "Preview of chart.png" }).getAttribute("href");
  assert.match(fileHref, new RegExp(`^/api/conversations/${id}/files/`));
  assert.match(imageHref, new RegExp(`^/api/conversations/${id}/files/`));
  const text = await context.request.get(new URL(fileHref, ctx.BASE).href);
  assert.equal(text.status(), 200);
  assert.equal(await text.text(), "Quarterly notes: revenue up 4 percent.\n");
  const image = await context.request.get(new URL(imageHref, ctx.BASE).href);
  assert.equal(image.status(), 200);
  assert.equal(image.headers()["content-type"], "image/png");
  assert.equal((await image.body()).subarray(1, 4).toString(), "PNG");
  // A file (not an image) is served as an attachment: clicking it downloads it.
  const downloaded = new Promise((resolve) => {
    const onPage = (p) => p.on("download", (d) => {
      context.off("page", onPage);
      resolve({ download: d, opened: p });
    });
    context.on("page", onPage);
    page.on("download", (d) => resolve({ download: d, opened: null }));
  });
  await fileLink.click();
  const { download, opened } = await downloaded;
  assert.equal(download.suggestedFilename(), "notes.txt");
  const saved = path.join(WORK, "downloads", `${id}-notes.txt`);
  await download.saveAs(saved);
  assert.equal(fs.readFileSync(saved, "utf8"), "Quarterly notes: revenue up 4 percent.\n");
  await opened?.close().catch(() => {});

  step("Another signed-in person can't download them");
  const sam = await ctx.signedIn(SAM);
  for (const href of [fileHref, imageHref]) {
    const res = await sam.context.request.get(new URL(href, ctx.BASE).href);
    assert.ok([403, 404].includes(res.status()), `Sam got ${res.status()} for ${href}`);
  }
  const anon = await ctx.browser.newContext();
  const res = await anon.request.get(new URL(fileHref, ctx.BASE).href);
  assert.equal(res.status(), 401);
  await anon.close();
  await sam.context.close();
  assert.ok(id);
  await context.close();
});

journey("artifact", "A file the agent writes: a download link that works with the session, for its owner only", async (ctx) => {
  const { page, context } = await ctx.signedIn(ADMIN);
  ctx.setDiagnostic(page);
  const c = ctx.chat(page);
  await c.settled();
  step("The artifact keyword writes a file and a download link appears");
  await c.send("Please write the artifact report");
  await page.waitForURL(/\/c\//);
  await c.idle();
  const id = c.conversationId();
  await page.getByTestId("tool-entry").filter({ hasText: "write_artifact" }).getByText("Done").waitFor();
  const link = c.lastAssistant().getByRole("link", { name: /scripted-report\.md/ });
  await link.waitFor();
  const href = await link.getAttribute("href");
  const url = new URL(href, ctx.BASE);
  assert.equal(url.origin, ctx.BASE, `the link points at this server (${href})`);
  assert.equal(url.pathname, `/artifacts/web-${id}/scripted-report.md`);
  assert.equal(await link.getAttribute("aria-label"), "Download scripted-report.md");
  await ctx.shot(page, "14-artifact-link");

  step("Clicking it downloads the file");
  const [download] = await Promise.all([page.waitForEvent("download"), link.click()]);
  assert.equal(download.suggestedFilename(), "scripted-report.md");
  const saved = path.join(WORK, "downloads", `${id}-scripted-report.md`);
  await download.saveAs(saved);
  assert.match(fs.readFileSync(saved, "utf8"), /^# Scripted report/);

  step("The owner's session is enough: no token needed");
  const bare = `${ctx.BASE}/artifacts/web-${encodeURIComponent(id)}/scripted-report.md`;
  const own = await context.request.get(bare);
  assert.equal(own.status(), 200);
  assert.match(await own.text(), /^# Scripted report/);

  step("Another signed-in person can't download it without the link's token, nor can a stranger");
  const sam = await ctx.signedIn(SAM);
  const theirs = await sam.context.request.get(bare);
  assert.ok([401, 403, 404].includes(theirs.status()), `Sam got ${theirs.status()}`);
  await sam.context.close();
  const anon = await ctx.browser.newContext();
  const stranger = await anon.request.get(bare);
  assert.ok([401, 403, 404].includes(stranger.status()), `a stranger got ${stranger.status()}`);
  await anon.close();
  await context.close();
});


journey("sidebar", "Sidebar: rename, search by title and text, archive, restore, delete with the files", async (ctx) => {
  const { page, context } = await ctx.signedIn(ADMIN);
  ctx.setDiagnostic(page);
  const c = ctx.chat(page);
  await c.settled();
  const sidebar = c.sidebar();
  const stamp = Date.now().toString(36);

  step("Two chats to work with: one with a file the agent wrote");
  await c.send(`Sidebar pelican ${stamp} please write the artifact`);
  await page.waitForURL(/\/c\//);
  await c.idle();
  const id = c.conversationId();
  await page.getByTestId("tool-entry").filter({ hasText: "write_artifact" }).getByText("Done").waitFor();
  const chatDir = path.join(ctx.accounts.hub, ".hubzoid", "chats", `web-${id}`);
  assert.ok(fs.existsSync(path.join(chatDir, "artifacts", "scripted-report.md")), "the agent's file is on disk");
  await page.getByRole("button", { name: "New chat" }).click();
  await c.settled();
  await c.send(`Second sidebar chat ${stamp}`);
  await page.waitForURL(/\/c\//);
  await c.idle();
  const otherId = c.conversationId();
  const row = (convId) => sidebar.locator(`li:has(a[href="/c/${encodeURIComponent(convId)}"])`);
  await row(id).waitFor();

  step("Rename inline from the row menu");
  const title = `Pelican plan ${stamp}`;
  await row(id).getByRole("button", { name: /^Options for / }).click();
  await page.getByRole("menuitem", { name: "Rename" }).click();
  const field = sidebar.getByRole("textbox", { name: "Chat title" });
  await field.fill(title);
  await field.press("Enter");
  await sidebar.getByRole("link", { name: title }).waitFor();
  await page.getByText("Chat renamed.").waitFor();
  assert.equal((await ctx.apiGet(context, `/api/conversations/${id}`)).json.conversation.title, title);

  step("Search finds a chat by its title and by words inside its messages");
  const search = sidebar.getByRole("searchbox", { name: "Search chats" });
  await search.fill("pelican plan");
  await sidebar.getByRole("link", { name: title }).waitFor();
  await page.waitForFunction(() => document.querySelectorAll('nav[aria-label="Conversations"] li a').length === 1);
  await search.fill(`second sidebar chat ${stamp}`);
  await row(otherId).waitFor();
  await page.waitForFunction(() => document.querySelectorAll('nav[aria-label="Conversations"] li a').length === 1);
  await search.fill("You said"); // words only in the replies
  await row(id).waitFor();
  await row(otherId).waitFor();
  await search.fill(`nothing-matches-${stamp}`);
  await sidebar.getByText(`No chats match “nothing-matches-${stamp}”.`).waitFor();
  await ctx.shot(page, "15-sidebar-search");
  await sidebar.getByRole("button", { name: "Clear search" }).click();
  await row(otherId).waitFor();

  step("Archive hides it; the Archived view lists it; Restore brings it back");
  await row(id).getByRole("button", { name: /^Options for / }).click();
  await page.getByRole("menuitem", { name: "Archive" }).click();
  await page.getByText("Chat archived.").waitFor();
  await row(id).waitFor({ state: "detached" });
  assert.equal((await ctx.apiGet(context, `/api/conversations/${id}`)).json.conversation.archived, true);
  await sidebar.getByRole("button", { name: "Archived chats" }).click();
  await sidebar.getByRole("heading", { name: "Archived" }).waitFor();
  await row(id).waitFor();
  assert.equal(await row(otherId).count(), 0);
  await ctx.shot(page, "16-archived");
  await row(id).getByRole("button", { name: /^Options for / }).click();
  await page.getByRole("menuitem", { name: "Restore" }).click();
  await page.getByText("Chat restored.").waitFor();
  await row(id).waitFor({ state: "detached" });
  await sidebar.getByRole("button", { name: "Back to chats" }).click();
  await row(id).waitFor();
  assert.equal((await ctx.apiGet(context, `/api/conversations/${id}`)).json.conversation.archived, false);

  step("Delete asks first, then removes the chat, its messages and its files on disk");
  await page.goto(`${ctx.BASE}/c/${encodeURIComponent(id)}`);
  await c.settled();
  await row(id).getByRole("button", { name: /^Options for / }).click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Delete this chat?" });
  await dialog.getByText(`“${title}” and its files will be deleted for good.`, { exact: false }).waitFor();
  await ctx.shot(page, "17-delete-confirm");
  await dialog.getByRole("button", { name: "Delete chat" }).click();
  await page.getByText("Chat deleted.").waitFor();
  await row(id).waitFor({ state: "detached" });
  await page.waitForURL(`${ctx.BASE}/`);
  assert.equal((await ctx.apiGet(context, `/api/conversations/${id}`)).status, 404);
  assert.equal(fs.existsSync(chatDir), false, "the chat's folder is gone from disk");
  const gone = await context.request.get(`${ctx.BASE}/artifacts/web-${encodeURIComponent(id)}/scripted-report.md`);
  assert.ok([401, 403, 404].includes(gone.status()), `the deleted chat's file answers ${gone.status()}`);
  await page.goto(`${ctx.BASE}/c/${encodeURIComponent(id)}`);
  await page.getByRole("heading", { name: "This chat doesn't exist or was deleted." }).waitFor();
  await context.close();
});

journey("share", "Share: a read-only link another person opens, until it is turned off", async (ctx) => {
  const { page, context } = await ctx.signedIn(ADMIN);
  ctx.setDiagnostic(page);
  const c = ctx.chat(page);
  await c.settled();
  step("A chat with a tool call and a markdown reply to share");
  await c.send("Use a tool and answer in markdown");
  await page.waitForURL(/\/c\//);
  await c.idle();
  const id = c.conversationId();
  await c.lastAssistant().getByRole("table").waitFor();

  step("Create link: the dialog shows and copies the link");
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: ctx.BASE });
  await page.getByRole("button", { name: "Share", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Share chat" });
  await dialog.getByRole("button", { name: "Create link" }).click();
  const field = dialog.getByRole("textbox", { name: "Share link" });
  await field.waitFor();
  const shareUrl = await field.inputValue();
  assert.match(shareUrl, new RegExp(`^${ctx.BASE}/s/[A-Za-z0-9_-]+$`));
  await page.getByText("Link copied.").waitFor();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), shareUrl);
  await ctx.shot(page, "18-share-dialog");
  await ctx.axe(page, "share dialog");
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "detached" });

  step("A second person signs in from the link and reads the conversation, read-only");
  const sam = await ctx.open();
  await sam.page.goto(shareUrl);
  await sam.page.waitForURL(/\/auth\?redirect=%2Fs%2F/);
  await ctx.signIn(sam.page, SAM);
  await sam.page.waitForURL(shareUrl);
  const title = (await ctx.apiGet(context, `/api/conversations/${id}`)).json.conversation.title;
  await sam.page.getByRole("heading", { name: title, level: 1 }).waitFor();
  await sam.page.getByTestId("share-byline").getByText(new RegExp(`^Shared by ${ADMIN.name} · `)).waitFor();
  await sam.page.getByTestId("tool-entry").getByText("read_knowledge").waitFor();
  await sam.page.locator('[data-role="assistant"]').getByRole("table").waitFor();
  assert.equal(await sam.page.getByRole("textbox", { name: /^Message / }).count(), 0, "no composer");
  assert.equal(await sam.page.getByRole("button", { name: "Edit message" }).count(), 0, "no edit");
  assert.equal(await sam.page.getByRole("button", { name: "Regenerate response" }).count(), 0, "no regenerate");
  await ctx.shot(sam.page, "19-shared-view");
  await ctx.axe(sam.page, "shared view");
  // The owner's own chat is still private to them.
  assert.equal((await ctx.apiGet(sam.context, `/api/conversations/${id}`)).status, 404);

  step("Later messages are not in the shared copy");
  await c.send("A message after sharing");
  await c.idle();
  await sam.page.reload();
  await sam.page.getByRole("heading", { name: title, level: 1 }).waitFor();
  assert.equal(await sam.page.getByText("A message after sharing").count(), 0);

  step("Turn off link: the link stops working");
  await page.getByRole("button", { name: "Share", exact: true }).click();
  await dialog.getByRole("button", { name: "Turn off link" }).click();
  await page.getByText("Link turned off. It no longer opens this chat.").waitFor();
  await page.keyboard.press("Escape");
  await sam.page.reload();
  await sam.page.getByRole("heading", { name: "Link not available" }).waitFor();
  await ctx.shot(sam.page, "20-share-revoked");
  const shareId = shareUrl.split("/s/")[1];
  assert.equal((await ctx.apiGet(sam.context, `/api/shares/${shareId}`)).status, 404);
  await sam.context.close();
  await context.close();
});

journey("account", "Account: display name, password change ends other sessions, sign in again", async (ctx) => {
  // A person of their own, so the password change can't disturb other journeys.
  const who = { email: "lee@example.com", password: "lee-first-pass-1", name: "Lee Park" };
  await createMember(ctx, who);
  const first = await ctx.signedIn(who);
  ctx.setDiagnostic(first.page);
  const other = await ctx.signedIn(who); // a second device

  step("Change the display name: the sidebar shows it at once");
  await first.page.goto(`${ctx.BASE}/account`);
  await first.page.getByRole("heading", { name: "Account", level: 1 }).waitFor();
  await ctx.axe(first.page, "account");
  await first.page.getByLabel("Display name").fill("Lee P. Park");
  await first.page.getByRole("button", { name: "Save", exact: true }).click();
  await first.page.getByText("Name saved.").waitFor();
  await first.page.getByRole("navigation", { name: "Conversations" }).getByText("Lee P. Park").waitFor();
  const me = (await ctx.apiGet(first.context, "/api/auth/session")).json;
  assert.equal(me.user.name, "Lee P. Park");

  step("A wrong current password is refused in plain words");
  await first.page.getByLabel("Current password").fill("not-my-password");
  await first.page.getByLabel("New password", { exact: true }).fill("lee-second-pass-2");
  await first.page.getByLabel("Confirm new password").fill("lee-second-pass-2");
  await first.page.getByRole("button", { name: "Change password" }).click();
  await first.page.getByText("Your current password isn't right.").waitFor();

  step("Change the password: this session stays, the other device is signed out");
  await first.page.getByLabel("Current password").fill(who.password);
  await first.page.getByRole("button", { name: "Change password" }).click();
  await first.page.getByText("Password changed. Your other sessions were signed out.").waitFor();
  await ctx.shot(first.page, "21-account");
  assert.equal((await ctx.apiGet(first.context, "/api/auth/session")).json.authenticated, true);
  await other.page.goto(`${ctx.BASE}/account`);
  await other.page.waitForURL(/\/auth\?redirect=%2Faccount$/);

  step("The old password no longer works; the new one does");
  await ctx.signIn(other.page, who);
  await other.page.getByText("That email and password don't match. Check them and try again.").waitFor();
  await ctx.signIn(other.page, { ...who, password: "lee-second-pass-2" });
  await other.page.waitForURL(`${ctx.BASE}/account`);
  await other.page.getByLabel("Display name").waitFor();
  assert.equal(await other.page.getByLabel("Display name").inputValue(), "Lee P. Park");
  await other.context.close();
  await first.context.close();
});

/** A member account made through the Console API and its one-time link. */
async function createMember(ctx, who, grants = null) {
  const hubs = await (await ctx.setup.get("/portal/api/hubs")).json();
  const created = await ctx.setup.post("/portal/api/accounts", {
    data: {
      email: who.email,
      name: who.name,
      sign_in: "password",
      grants: grants ?? [{ hub: hubs.hubs[0].key, permission: "use_hub" }],
    },
  });
  const body = await created.json();
  assert.ok(created.ok() && body.link, `Add user ${who.email}: ${created.status()} ${JSON.stringify(body)}`);
  const token = new URL(body.link, ctx.BASE).searchParams.get("token");
  const anon = await httpRequest.newContext({ baseURL: ctx.BASE, extraHTTPHeaders: { Origin: ctx.BASE } });
  const set = await anon.post(`/api/auth/link/${encodeURIComponent(token)}`, { data: { password: who.password } });
  assert.equal(set.status(), 200, `${who.email} sets a password: ${await set.text()}`);
  await anon.dispose();
}


journey("people", "Console People: Add user with a one-time link, access, Reset password", async (ctx) => {
  const who = { email: "noor@example.com", name: "Noor Haddad", password: "noor-first-pass-1" };
  const admin = await ctx.signedIn(ADMIN);
  ctx.setDiagnostic(admin.page);
  const page = admin.page;
  await admin.context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: ctx.BASE });

  step("The administrator opens People in the Console and adds a user with no access yet");
  await page.goto(`${ctx.BASE}/portal/#/people`);
  await page.getByRole("button", { name: "Add user" }).click();
  const drawer = page.getByRole("dialog").filter({ hasText: "Add user" });
  await drawer.getByLabel("Name").fill(who.name);
  await drawer.getByLabel("Email address").fill(who.email);
  await ctx.shot(page, "22-console-add-user");
  await drawer.getByRole("button", { name: "Review" }).click();
  await page.getByRole("button", { name: "Create account" }).click();
  const done = page.getByRole("dialog").filter({ hasText: "User added" });
  await done.waitFor();
  const linkField = done.locator("#one-time-link");
  const link = await linkField.inputValue();
  assert.match(link, /\/auth\/set-password\?token=/);
  await done.getByRole("button", { name: "Copy sign-in link" }).click();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), link);
  await ctx.shot(page, "23-console-user-added");
  await done.getByRole("button", { name: "Done" }).click();

  step("The link, opened signed out, sets a password and signs them in");
  const invited = await ctx.open();
  await invited.page.goto(link);
  await invited.page.getByRole("heading", { name: "Set your password" }).waitFor();
  await invited.page.getByText(`For ${who.email}`).waitFor();
  assert.ok(!invited.page.url().includes("token="), "the token leaves the address bar");
  await ctx.axe(invited.page, "set password");
  await ctx.shot(invited.page, "24-set-password");
  await invited.page.getByLabel("New password").fill(who.password);
  await invited.page.getByLabel("Confirm password").fill(who.password);
  await invited.page.getByRole("button", { name: "Save password and sign in" }).click();
  await invited.page.waitForURL(`${ctx.BASE}/`);

  step("Without access to any agent they see how to get access");
  await invited.page.getByText("You don't have access to an agent yet. Ask an administrator to give you access, then reload this page.").waitFor();
  await ctx.shot(invited.page, "25-no-agents");
  assert.equal((await ctx.apiGet(invited.context, "/api/agents")).json.agents.length, 0);

  step("The administrator gives them access to the agent in the Console");
  const consoleDrawer = () => page.locator(".ant-drawer-section[role=dialog]");
  await page.goto(`${ctx.BASE}/portal/#/people/${encodeURIComponent(who.email)}`);
  await consoleDrawer().getByText("No access to any agent you manage.").waitFor();
  await ctx.shot(page, "26-console-person");
  await consoleDrawer().getByRole("combobox", { name: "Add an agent" }).click();
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter({ hasText: AGENT_NAME }).click();
  await page.waitForURL(/#\/agents\/[^/]+\/access/);
  await consoleDrawer().getByText(who.name).first().waitFor();
  assert.equal(await consoleDrawer().getByRole("checkbox", { name: /Use this agent/ }).isChecked(), true);
  await ctx.shot(page, "27-console-access-editor");
  await consoleDrawer().getByRole("button", { name: "Review changes" }).click();
  await consoleDrawer().getByRole("button", { name: "Save change" }).click();
  await consoleDrawer().waitFor({ state: "hidden" });
  await page.getByText("Access updated", { exact: true }).waitFor();

  step("After a reload the agent is there for them");
  await invited.page.reload();
  await invited.page.getByRole("heading", { name: `What can ${AGENT_NAME} help with?` }).waitFor();
  const chatAsNoor = ctx.chat(invited.page);
  await chatAsNoor.settled();
  await chatAsNoor.send("Hello from Noor");
  await chatAsNoor.idle();
  await chatAsNoor.lastAssistant().getByText("You said: “Hello from Noor”").waitFor();

  step("Reset password ends their sessions and makes a new one-time link");
  await page.goto(`${ctx.BASE}/portal/#/people/${encodeURIComponent(who.email)}`);
  await consoleDrawer().getByRole("button", { name: "Reset password" }).click();
  await consoleDrawer().getByRole("button", { name: "Reset and create sign-in link" }).click();
  const resetLink = await consoleDrawer().locator("#one-time-link").inputValue();
  assert.match(resetLink, /\/auth\/set-password\?token=/);
  assert.notEqual(resetLink, link);
  await ctx.shot(page, "28-console-reset-link");
  await invited.page.reload();
  await invited.page.waitForURL(/\/auth/);

  step("The used first link is refused; the new link sets a new password and signs in");
  await invited.page.goto(link);
  await invited.page.getByRole("heading", { name: "This link can't be used" }).waitFor();
  await invited.page.goto(resetLink);
  await invited.page.getByRole("heading", { name: "Choose a new password" }).waitFor();
  await invited.page.getByLabel("New password").fill("noor-second-pass-2");
  await invited.page.getByLabel("Confirm password").fill("noor-second-pass-2");
  await invited.page.getByRole("button", { name: "Save password and sign in" }).click();
  await invited.page.waitForURL(`${ctx.BASE}/`);
  await invited.page.getByRole("heading", { name: `What can ${AGENT_NAME} help with?` }).waitFor();
  await invited.page.getByRole("navigation", { name: "Conversations" }).getByRole("link", { name: /hello from noor/i }).waitFor();
  await invited.context.close();
  await admin.context.close();
});


journey("console", "Console: reachable from the account menu for administrators only; its Sign out signs out", async (ctx) => {
  step("A member's account menu has no Admin Console, and /portal/ turns them away");
  const sam = await ctx.signedIn(SAM);
  ctx.setDiagnostic(sam.page);
  await sam.page.getByRole("button", { name: /^Account menu: / }).click();
  await sam.page.getByRole("menuitem", { name: "Account" }).waitFor();
  assert.equal(await sam.page.getByRole("menuitem", { name: "Admin Console" }).count(), 0);
  await sam.page.keyboard.press("Escape");
  await sam.page.goto(`${ctx.BASE}/portal/`);
  await sam.page.waitForTimeout(1500);
  await ctx.shot(sam.page, "29-console-member");
  const me = await sam.context.request.get(`${ctx.BASE}/portal/api/me`);
  assert.equal(me.status(), 403);
  await sam.context.close();

  step("An administrator opens the Console from the account menu");
  const ada = await ctx.signedIn(ADMIN);
  ctx.setDiagnostic(ada.page);
  const page = ada.page;
  await page.getByRole("button", { name: /^Account menu: / }).click();
  await page.getByRole("menuitem", { name: "Admin Console" }).click();
  await page.waitForURL(`${ctx.BASE}/portal/`);
  await page.getByRole("link", { name: "People" }).waitFor();
  assert.equal(await page.title(), "Hubzoid Admin Console");
  await ctx.shot(page, "30-console-home");

  step("Open chat goes back to the chat app, still signed in");
  await page.getByRole("link", { name: /Open chat/ }).click();
  await page.waitForURL(`${ctx.BASE}/`);
  await page.getByRole("heading", { name: `What can ${AGENT_NAME} help with?` }).waitFor();

  step("The Console's Sign out signs out of Hubzoid");
  await page.goto(`${ctx.BASE}/portal/`);
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.waitForURL(/\/auth/);
  await page.getByRole("heading", { name: `Sign in to ${AGENT_NAME}` }).waitFor();
  assert.equal((await ctx.apiGet(ada.context, "/api/auth/session")).json.authenticated, false);
  await page.goto(`${ctx.BASE}/portal/`);
  await page.waitForTimeout(1000);
  assert.equal((await ada.context.request.get(`${ctx.BASE}/portal/api/me`)).status(), 401);
  await ada.context.close();
});


journey("connections", "Connections: empty state, a connector to a second hub's MCP server, Connect and Disconnect", async (ctx) => {
  const ada = await ctx.signedIn(ADMIN, "/account/connections");
  ctx.setDiagnostic(ada.page);
  const page = ada.page;

  step("Before any connector exists the page says how to get one");
  await page.getByRole("heading", { name: "Connections", level: 1 }).waitFor();
  await page.getByText("No connections are set up yet. An administrator can add them in the Admin Console.").waitFor();
  await ctx.shot(page, "31-connections-empty");
  await ctx.axe(page, "connections");

  step("The administrator registers the second hub's MCP server in the Console");
  await page.goto(`${ctx.BASE}/portal/#/connectors`);
  await page.getByRole("button", { name: "Add connector" }).first().click();
  const editor = page.locator(".ant-drawer-section[role=dialog]");
  await editor.getByLabel("Name").fill("Remote Notes");
  await editor.getByLabel("Server URL").fill(`${ctx.mcp.base}/mcp`);
  await ctx.shot(page, "32-console-add-connector");
  await editor.getByRole("button", { name: "Add connector" }).click();
  // Saving tests the server at once: discovery on the real MCP hub.
  const test = page.getByRole("dialog", { name: "Test Remote Notes" });
  await test.getByText("Ready", { exact: true }).waitFor();
  await test.getByText("Discovery succeeded. People can connect with their own account.").waitFor();
  await ctx.shot(page, "33-console-connector-ready");
  await page.keyboard.press("Escape");
  await test.waitFor({ state: "hidden" });

  step("On a Console-managed agent, using it is a capability: the administrator gives it to Sam");
  await page.goto(`${ctx.BASE}/account/connections`);
  await page.getByTestId(/^connection-/).filter({ hasText: "Remote Notes" }).getByText("Not available to you").waitFor();
  const hubKey = (await (await ctx.setup.get("/portal/api/hubs")).json()).hubs[0].key;
  await page.goto(`${ctx.BASE}/portal/#/agents/${hubKey}/access`);
  await page.getByRole("button", { name: `Edit access for ${SAM.name}` }).click();
  const access = page.locator(".ant-drawer-section[role=dialog]");
  await access.getByText(SAM.name).first().waitFor();
  // Connector capabilities sit with the agent's tools.
  const tools = access.getByRole("button", { name: /^Hubzoid tools/ });
  if ((await tools.getAttribute("aria-expanded")) !== "true") await tools.click();
  await access.getByRole("checkbox", { name: /Connect Remote Notes/ }).check();
  await ctx.shot(page, "33b-console-grant-connector");
  await access.getByRole("button", { name: "Review changes" }).click();
  await access.getByRole("button", { name: "Save change" }).click();
  await access.waitFor({ state: "hidden" });
  await page.getByText("Access updated", { exact: true }).waitFor();
  await ada.context.close();
  const sam = await ctx.signedIn(SAM, "/account/connections");
  ctx.setDiagnostic(sam.page);
  const page2 = sam.page;

  step("Sam connects it through the other hub's sign-in and consent page");
  await page2.getByRole("heading", { name: "Connections", level: 1 }).waitFor();
  const row = page2.getByTestId(/^connection-/).filter({ hasText: "Remote Notes" });
  await row.getByText("Not connected").waitFor();
  await ctx.shot(page2, "34-connections-not-connected");
  await row.getByRole("button", { name: "Connect Remote Notes" }).click();
  await page2.waitForURL((url) => url.host === new URL(ctx.mcp.base).host);
  // Not signed in on the other hub yet: its own sign-in page comes first.
  await page2.waitForURL(/\/auth\?redirect=%2Fmcp%2F/);
  await page2.getByRole("heading", { name: "Sign in to Remote Notes" }).waitFor();
  await ctx.shot(page2, "35-mcp-hub-sign-in");
  await ctx.signIn(page2, MCP_OWNER);
  // Back to the other hub's own consent page (a server page, not the chat app's).
  await page2.waitForURL(/\/mcp\/oauth\/consent\?ticket=/);
  await page2.getByRole("heading", { name: "Connect your assistant" }).waitFor();
  await page2.getByText(`Signed in as ${MCP_OWNER.email}.`).waitFor();
  await ctx.shot(page2, "36-mcp-consent");
  await page2.getByRole("button", { name: "Allow connection" }).click();

  step("Back on Hubzoid the connection shows as connected");
  await page2.waitForURL(`${ctx.BASE}/account/connections?connected=remote_notes`);
  await page2.getByText("Connected to Remote Notes.").waitFor();
  await row.getByText("Connected", { exact: true }).waitFor();
  await ctx.shot(page2, "37-connections-connected");
  const listed = (await ctx.apiGet(sam.context, "/api/connections")).json;
  const mine = (Array.isArray(listed) ? listed : listed.connections ?? listed.items ?? []).find((c) => c.connector_id === "remote_notes");
  assert.equal(mine?.connected, true, JSON.stringify(listed));

  step("Disconnect asks first, then the connection is gone");
  await row.getByRole("button", { name: "Disconnect Remote Notes" }).click();
  const confirm = page2.getByRole("alertdialog", { name: "Disconnect Remote Notes?" });
  await ctx.shot(page2, "38-connections-disconnect");
  await confirm.getByRole("button", { name: "Disconnect" }).click();
  await page2.getByText("Disconnected from Remote Notes.").waitFor();
  await row.getByText("Not connected").waitFor();
  const after = (await ctx.apiGet(sam.context, "/api/connections")).json;
  const gone = (Array.isArray(after) ? after : after.connections ?? after.items ?? []).find((c) => c.connector_id === "remote_notes");
  assert.equal(gone?.connected, false, JSON.stringify(after));
  await sam.context.close();
});


journey("devices", "Phone (360 px) chat and drawer, dark mode, keyboard-only send", async (ctx) => {
  step("On a 360 px phone: no sideways scroll, a chat works, the sidebar is a drawer Escape closes");
  const phone = await ctx.open({ viewport: { width: 360, height: 740 }, isMobile: true, hasTouch: true });
  ctx.setDiagnostic(phone.page);
  const m = phone.page;
  await m.goto(`${ctx.BASE}/`);
  await m.waitForURL(/\/auth/);
  await ctx.signIn(m, SAM);
  await m.getByRole("heading", { name: `What can ${AGENT_NAME} help with?` }).waitFor();
  await ctx.noHorizontalScroll(m, "phone new chat");
  await ctx.shot(m, "39-phone-new-chat");
  await m.getByRole("textbox", { name: /^Message / }).fill("Please answer in markdown");
  await m.getByRole("button", { name: "Send message" }).tap();
  await m.waitForURL(/\/c\//);
  await m.getByTestId("send").waitFor();
  await m.locator('[data-role="assistant"]').last().getByRole("table").waitFor();
  await ctx.noHorizontalScroll(m, "phone conversation with a table");
  await ctx.shot(m, "40-phone-conversation");
  await m.getByRole("button", { name: "Open navigation" }).click();
  const drawer = m.getByRole("dialog", { name: "Conversations" });
  await drawer.getByRole("link", { name: /About please answer in/ }).waitFor();
  await ctx.shot(m, "41-phone-drawer");
  await m.keyboard.press("Escape");
  await drawer.waitFor({ state: "detached" });
  await phone.context.close();

  step("Dark mode from the account menu, kept after a reload");
  const ada = await ctx.signedIn(ADMIN);
  ctx.setDiagnostic(ada.page);
  const page = ada.page;
  const c = ctx.chat(page);
  await c.settled();
  await c.send("Use a tool and answer in markdown");
  await page.waitForURL(/\/c\//);
  await c.idle();
  await page.getByRole("button", { name: /^Account menu: / }).click();
  await page.getByRole("menuitemradio", { name: "Dark" }).click();
  await page.keyboard.press("Escape");
  assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark");
  await page.reload();
  await c.lastAssistant().getByRole("table").waitFor();
  await c.lastAssistant().locator(".shiki").first().waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark");
  await ctx.shot(page, "42-dark-conversation");
  await ctx.axe(page, "conversation (dark)");
  await page.goto(`${ctx.BASE}/`);
  await c.settled();
  await ctx.shot(page, "43-dark-new-chat");
  await page.getByRole("button", { name: /^Account menu: / }).click();
  await page.getByRole("menuitemradio", { name: "Light" }).click();
  await page.keyboard.press("Escape");

  step("Keyboard only: Tab to the composer, Shift+Enter for a new line, Enter to send");
  await page.locator("body").click({ position: { x: 700, y: 5 } });
  let reached = false;
  for (let i = 0; i < 40 && !reached; i++) {
    await page.keyboard.press("Tab");
    reached = await page.evaluate(() => document.activeElement?.getAttribute("aria-label")?.startsWith("Message ") ?? false);
  }
  assert.ok(reached, "the composer is reachable with Tab");
  await page.keyboard.type("line one");
  await page.keyboard.press("Shift+Enter");
  await page.keyboard.type("line two");
  await page.keyboard.press("Enter");
  await page.getByTestId("user-text").getByText(/line one\s+line two/).waitFor();
  await c.idle();
  await c.lastAssistant().getByText("You said: “line one line two”").waitFor();
  await ada.context.close();
});

journey("signout", "Sign out from the account menu ends the session", async (ctx) => {
  const sam = await ctx.signedIn(SAM);
  ctx.setDiagnostic(sam.page);
  step("Sign out from the account menu goes to sign-in, and the session is over");
  await sam.page.getByRole("button", { name: /^Account menu: / }).click();
  await sam.page.getByRole("menuitem", { name: "Sign out" }).click();
  await sam.page.waitForURL(`${ctx.BASE}/auth`);
  assert.equal((await ctx.apiGet(sam.context, "/api/auth/session")).json.authenticated, false);
  await sam.page.goto(`${ctx.BASE}/account`);
  await sam.page.waitForURL(/\/auth\?redirect=%2Faccount$/);
  await sam.context.close();
});

journey("local", "Local mode: no sign-in page and no Sign out", async (ctx) => {
  const { page, context } = await ctx.open();
  ctx.setDiagnostic(page);
  step("The local owner lands in chat; /auth goes back to chat");
  await page.goto(`${ctx.local.base}/`);
  await page.getByRole("heading", { name: "What can Local Helper help with?" }).waitFor();
  await page.goto(`${ctx.local.base}/auth`);
  await page.waitForURL(`${ctx.local.base}/`);
  const c = ctx.chat(page);
  await c.settled();
  await c.send("Hello from local mode");
  await c.idle();
  await c.lastAssistant().getByText("You said: “Hello from local mode”").waitFor();
  step("The account menu has the Console and no Sign out; the account page explains why");
  await page.getByRole("button", { name: /^Account menu: Local mode/ }).click();
  await page.getByRole("menuitem", { name: "Admin Console" }).waitFor();
  assert.equal(await page.getByRole("menuitem", { name: "Sign out" }).count(), 0);
  await page.keyboard.press("Escape");
  await page.goto(`${ctx.local.base}/account`);
  await page.getByText(/Sign-in is off on this server/).waitFor();
  assert.equal(await page.getByRole("button", { name: "Sign out" }).count(), 0);
  await ctx.shot(page, "44-local-mode-account");
  await context.close();
});

main();
