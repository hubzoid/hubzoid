// Feedback journeys for the Hubzoid web app against a REAL Hubzoid server
// (proposals/2026-10-20-feedback-across-channels.md).
//
// A companion to app-real-journey.cjs, using its helpers: one server on a
// fresh scratch hub with the scripted test model (no model, no network, no
// cost), sign-in on, and one headless browser. Slack runs against a fake Slack
// Web API served here (signed requests in, nothing sent to Slack). WhatsApp is
// simulated in process while the server is stopped (feedback_whatsapp_sim.py,
// a fake Meta API), which is also the restart check. Screenshots of the
// channel records are labelled local simulation.
//
//   npm run build && npm run test:app:feedback
//
// Settings: HZ_PYTHON, APP_REAL_DIR and APP_REAL_PORT as app-real-journey.cjs
// (this uses two ports from APP_REAL_PORT, default 4150).
const { chromium, request: httpRequest } = require("playwright");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const h = require("./app-real-journey.cjs");

const { WORK, SHOTS, ADMIN, SAM, AGENT_NAME, step, steps } = h;
const COMMENT = "Synthetic check fb-e2e-7731: the September price list is missing";
const SLACK_SECRET = "e2e-slack-signing-secret";
const shots = [];
let toolTarget = null;

// ---- a fake Slack Web API (what the hub's Slack client calls) ---------------------------
function fakeSlack(port) {
  const calls = [];
  let n = 0;
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const method = req.url.replace(/^\/+/, "").split("?")[0];
      let args = {};
      try {
        args = body.trim().startsWith("{") ? JSON.parse(body) : Object.fromEntries(new URLSearchParams(body));
        if (typeof args.blocks === "string") args.blocks = JSON.parse(args.blocks);
        if (typeof args.view === "string") args.view = JSON.parse(args.view);
      } catch {
        /* keep the raw call */
      }
      calls.push({ method, args });
      const out = { ok: true };
      if (method === "chat.postMessage") Object.assign(out, { ts: `1800000000.${String(++n).padStart(6, "0")}`, channel: args.channel });
      if (method === "conversations.info") out.channel = { id: args.channel, is_member: true, is_ext_shared: false };
      if (method === "conversations.replies") out.messages = [];
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(out));
    });
  });
  return new Promise((resolve) => server.listen(port, "127.0.0.1", () => resolve({ server, calls })));
}

function slackSigned(raw) {
  const ts = String(Math.floor(Date.now() / 1000));
  const sig = "v0=" + crypto.createHmac("sha256", SLACK_SECRET).update(`v0:${ts}:${raw}`).digest("hex");
  return { "x-slack-request-timestamp": ts, "x-slack-signature": sig };
}

async function main() {
  let server = null;
  let browser = null;
  let slack = null;
  const errors = [];
  try {
    const [port, slackPort] = await h.freePorts(2, Number(process.env.APP_REAL_PORT || 4150));
    console.log(`Scratch folder: ${WORK}`);
    const hub = h.makeHub("feedback", `name: ${AGENT_NAME}`);
    slack = await fakeSlack(slackPort);
    const env = {
      HUBZOID_AUTH: "true",
      HUBZOID_ADMIN_EMAIL: ADMIN.email,
      HUBZOID_ADMIN_PASSWORD: ADMIN.password,
      HUBZOID_ADMIN_NAME: ADMIN.name,
      HUBZOID_MFA_REQUIRED: "none",
      HUBZOID_SLACK_API_URL: `http://127.0.0.1:${slackPort}`,
      SLACK_E2E_SIGNING: SLACK_SECRET,
      SLACK_E2E_BOT: "xoxb-e2e-fake-token",
    };
    server = new h.Server("feedback", hub, port, "127.0.0.1", env);
    server.start();
    await server.ready();
    const BASE = server.base;
    console.log(`Server up: ${BASE}`);
    browser = await chromium.launch({ headless: true });

    const shot = async (page, name, scenario, expected, observed, level = "real backend, scripted model") => {
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
      shots.push({ file: `${name}.png`, scenario, expected, observed, level });
    };
    const open = async (options = {}) => {
      const context = await browser.newContext({ viewport: { width: 1360, height: 900 }, ...options });
      const page = await context.newPage();
      page.on("pageerror", (e) => errors.push(`${page.url()} pageerror: ${e.message}`));
      page.on("console", (m) => {
        if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(`${page.url()}: ${m.text()}`);
      });
      return { context, page };
    };
    const signedIn = async (who, options = {}) => {
      const s = await open(options);
      await s.page.goto(`${BASE}/`);
      await s.page.waitForURL(/\/auth/);
      await s.page.getByLabel("Email").fill(who.email);
      await s.page.getByLabel("Password").fill(who.password);
      await s.page.getByRole("button", { name: "Sign in", exact: true }).click();
      await s.page.waitForURL((url) => !url.pathname.startsWith("/auth"));
      return s;
    };
    const composer = (page) => page.getByRole("textbox", { name: /^Message / });
    const ask = async (page, text) => {
      await composer(page).fill(text);
      await composer(page).press("Enter");
      await page.getByTestId("send").waitFor({ timeout: 60_000 });
      await page.waitForURL(/\/c\//);
    };
    const last = (page) => page.locator('[data-role="assistant"]').last();
    const hover = async (page) => {
      await last(page).hover();
      return last(page);
    };
    const convId = (page) => decodeURIComponent(new URL(page.url()).pathname.split("/c/")[1] || "");
    const json = async (context, url) => {
      const res = await context.request.get(`${BASE}${url}`);
      return { status: res.status(), body: res.ok() ? await res.json() : null };
    };

    // ---- accounts: Sam, an ordinary member; Ada, the administrator --------------------------
    const admin = await httpRequest.newContext({ baseURL: BASE, extraHTTPHeaders: { Origin: BASE } });
    const login = await admin.post("/api/auth/login", { data: { email: ADMIN.email, password: ADMIN.password } });
    assert.equal(login.status(), 200);
    const hubKey = (await (await admin.get("/portal/api/hubs")).json()).hubs[0].key;
    {
      const created = await admin.post("/portal/api/accounts", {
        data: { email: SAM.email, name: SAM.name, sign_in: "password", grants: [{ hub: hubKey, permission: "use_hub" }] },
      });
      const body = await created.json();
      const token = new URL(body.link, BASE).searchParams.get("token");
      const anon = await httpRequest.newContext({ baseURL: BASE, extraHTTPHeaders: { Origin: BASE } });
      assert.equal((await anon.post(`/api/auth/link/${encodeURIComponent(token)}`, { data: { password: SAM.password } })).status(), 200);
      await anon.dispose();
    }
    const setFeedback = async (values) => {
      const current = await admin.get("/api/chat/config");
      assert.equal(current.status(), 200, await current.text());
      const cur = await current.json();
      // This scratch hub has no hubzoid.yaml chat section: its settings are
      // owned by chat/config.yaml. Preserve every other active setting.
      const config = { ...cur.config, feedback: { ...cur.config.feedback, ...values } };
      const file = path.join(hub, "chat", "config.yaml");
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(`${file}.tmp`, JSON.stringify(config, null, 2) + "\n");
      fs.renameSync(`${file}.tmp`, file);
      if (!cur.config.feedback.tool && config.feedback.tool) {
        // The tool list is built at startup. A real restart is required to
        // add record_feedback; file writes do not promise a runtime reload.
        await server.stop();
        server = new h.Server("feedback", hub, port, "127.0.0.1", env);
        server.start();
        await server.ready();
      }
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline) {
        const res = await admin.get("/api/chat/config");
        assert.equal(res.status(), 200, await res.text());
        const active = await res.json();
        if (Object.entries(values).every(([key, value]) => active.config.feedback[key] === value)) return active;
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      throw new Error("chat/config.yaml feedback settings did not become active");
    };

    // ---- 1 to 4: rate, reload, edit, remove ----------------------------------------------------
    console.log("\n[web] Rate an answer, reload, edit and remove");
    const sam = await signedIn(SAM);
    const p = sam.page;
    step("Sam asks a question and gets a completed answer with rating controls");
    await ask(p, "What does the starter plan cost per month?");
    const cid = convId(p);
    await hover(p);
    await last(p).getByTestId("feedback-positive").waitFor();
    await shot(p, "01-answer-with-controls", "A completed answer", "Thumbs up and down under the answer",
      "Both controls visible on hover");
    step("Thumbs up is shown as chosen only once the server saved it, and survives a reload");
    await last(p).getByTestId("feedback-positive").click();
    await p.locator('[data-testid="feedback-positive"][aria-pressed="true"]').last().waitFor();
    let mine = (await json(sam.context, `/api/conversations/${encodeURIComponent(cid)}/feedback`)).body.items;
    assert.equal(mine.length, 1);
    assert.equal(mine[0].rating, "positive");
    await p.reload();
    await hover(p);
    await p.locator('[data-testid="feedback-positive"][aria-pressed="true"]').last().waitFor();
    await shot(p, "03-saved-after-reload", "Reload after rating", "Thumbs up still chosen",
      `Chosen after reload; API rating ${mine[0].rating}, revision ${mine[0].revision}`);

    step("A second answer: thumbs down with a reason and a comment");
    await ask(p, "And what does the team plan cost?");
    await hover(p);
    await last(p).getByTestId("feedback-negative").click();
    const form = p.getByTestId("feedback-form");
    await form.waitFor();
    await form.getByTestId("feedback-reason-missing_source").click();
    await form.getByTestId("feedback-comment").fill(COMMENT);
    await shot(p, "02-negative-feedback-form", "Not helpful", "Reasons and a comment field", "Missing source chosen, comment typed");
    await form.getByTestId("feedback-send").click();
    await form.waitFor({ state: "detached" });
    mine = (await json(sam.context, `/api/conversations/${encodeURIComponent(cid)}/feedback`)).body.items;
    const neg = mine.find((m) => m.rating === "negative");
    assert.deepEqual([neg.reason_ids, neg.comment], [["missing_source"], COMMENT]);

    step("After a reload the form opens with the saved reason and comment; editing keeps one record");
    await p.reload();
    await hover(p);
    await p.locator('[data-testid="feedback-negative"][aria-pressed="true"]').last().click();
    await form.waitFor();
    assert.equal(await form.getByTestId("feedback-comment").inputValue(), COMMENT);
    assert.equal(await form.getByTestId("feedback-reason-missing_source").getAttribute("aria-pressed"), "true");
    await form.getByTestId("feedback-comment").fill(`${COMMENT} (edited)`);
    await shot(p, "04-editing-feedback", "Edit after reload", "Saved reason and comment restored; Remove offered",
      "Form restored from the server; comment being edited");
    await form.getByTestId("feedback-send").click();
    await form.waitFor({ state: "detached" });
    const edited = (await json(sam.context, `/api/conversations/${encodeURIComponent(cid)}/feedback`)).body.items.find(
      (m) => m.rating === "negative");
    assert.equal(edited.revision, neg.revision + 1);
    assert.equal(edited.comment, `${COMMENT} (edited)`);

    step("A failed save keeps the draft and offers Try again");
    await hover(p);
    await p.locator('[data-testid="feedback-negative"][aria-pressed="true"]').last().click();
    await form.waitFor();
    await form.getByTestId("feedback-comment").fill(`${COMMENT} (after an outage)`);
    await p.route("**/feedback", (route) => (route.request().method() === "PUT"
      ? route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: { code: "unavailable", message: "" } }) })
      : route.continue()));
    await form.getByTestId("feedback-send").click();
    await form.getByTestId("feedback-problem").waitFor();
    assert.equal(await form.getByTestId("feedback-comment").inputValue(), `${COMMENT} (after an outage)`);
    await shot(p, "04c-failed-save-recovery", "The server fails a save", "The draft stays, with a reason and Try again",
      "Draft kept; problem shown; nothing marked saved");
    await p.unroute("**/feedback");
    await form.getByTestId("feedback-send").click();
    await form.waitFor({ state: "detached" });

    step("A rating changed in another window is never overwritten");
    const before = (await json(sam.context, `/api/conversations/${encodeURIComponent(cid)}/feedback`)).body.items.find(
      (m) => m.message_id === edited.message_id);
    await hover(p);
    await p.locator('[data-testid="feedback-negative"][aria-pressed="true"]').last().click();
    await form.waitFor();
    const other = await sam.context.request.put(
      `${BASE}/api/conversations/${encodeURIComponent(cid)}/messages/${encodeURIComponent(edited.message_id)}/feedback`,
      { headers: { Origin: BASE }, data: { expected_revision: before.revision, rating: "negative", reason_ids: ["incorrect"], comment: "from another window" } });
    assert.equal(other.status(), 200);
    await form.getByTestId("feedback-comment").fill("stale draft");
    await form.getByTestId("feedback-send").click();
    await form.getByTestId("feedback-problem").filter({ hasText: "another window" }).waitFor();
    const after = (await json(sam.context, `/api/conversations/${encodeURIComponent(cid)}/feedback`)).body.items.find(
      (m) => m.message_id === edited.message_id);
    assert.equal(after.comment, "from another window");
    await shot(p, "04d-conflict", "Saved elsewhere meanwhile", "Conflict explained; the other save kept",
      `Server kept "${after.comment}" (revision ${after.revision})`);
    await p.keyboard.press("Escape");

    step("Remove: the rating goes, and the server agrees");
    await p.reload();
    await hover(p);
    await p.locator('[data-testid="feedback-negative"][aria-pressed="true"]').last().click();
    await form.waitFor();
    await form.getByTestId("feedback-remove").click();
    await form.waitFor({ state: "detached" });
    await p.locator('[data-testid="feedback-negative"][aria-pressed="false"]').last().waitFor();
    assert.equal((await json(sam.context, `/api/conversations/${encodeURIComponent(cid)}/feedback`)).body.items.length, 1);
    await shot(p, "04b-removed", "Remove rating", "Neither thumb chosen on that answer", "Removed; only the first answer's rating remains");

    step("Keyboard: open the form with Enter, choose a reason with Space, save with Enter");
    await hover(p);
    await last(p).getByTestId("feedback-negative").focus();
    await p.keyboard.press("Enter");
    await form.waitFor();
    await form.getByTestId("feedback-reason-incorrect").focus();
    await p.keyboard.press("Space");
    await form.getByTestId("feedback-comment").focus();
    await p.keyboard.type(COMMENT);
    await form.getByTestId("feedback-send").focus();
    await p.keyboard.press("Enter");
    await form.waitFor({ state: "detached" });
    const kb = (await json(sam.context, `/api/conversations/${encodeURIComponent(cid)}/feedback`)).body.items.find(
      (m) => m.rating === "negative");
    assert.deepEqual([kb.reason_ids, kb.comment], [["incorrect"], COMMENT]);

    // ---- the tool, through a real chat run ---------------------------------------------------
    step("record_feedback (opt-in): Sam asks the agent to save negative feedback on its previous answer");
    await setFeedback({ tool: true });
    await ask(p, "What does the enterprise plan cost?");
    // The answer just given, found by what it says (the snapshot can lag the screen by a moment).
    let target = null;
    for (let i = 0; i < 40 && !target; i++) {
      const msgs = (await json(sam.context, `/api/conversations/${encodeURIComponent(cid)}`)).body.messages;
      target = msgs.find((m) => m.role === "assistant" && JSON.stringify(m.content || []).includes("enterprise plan"))?.id;
      if (!target) await new Promise((r) => setTimeout(r, 250));
    }
    await ask(p, "Save negative feedback on your previous answer. The price was outdated.");
    await last(p).getByText("Saved: negative feedback").waitFor();
    const viaTool = (await json(sam.context, `/api/conversations/${encodeURIComponent(cid)}/feedback`)).body.items.find(
      (m) => m.source === "tool");
    assert.equal(viaTool.message_id, target, "the tool rated the previous answer, not its own confirmation");
    assert.equal(viaTool.comment, "The price was outdated.");

    step("Repeated requests (negative, positive, negative) and a web edit keep rating only that answer");
    const mineOn = async () => (await json(sam.context, `/api/conversations/${encodeURIComponent(cid)}/feedback`)).body.items
      .filter((m) => m.message_id === target);
    const countAll = async () => (await json(sam.context, `/api/conversations/${encodeURIComponent(cid)}/feedback`)).body.items.length;
    const ratedBefore = await countAll();
    await ask(p, "Save positive feedback on your previous answer");
    await ask(p, "Save negative feedback on your previous answer. Still the old price.");
    let onTarget = await mineOn();
    assert.equal(onTarget.length, 1);
    assert.equal(onTarget[0].rating, "negative");
    assert.equal(await countAll(), ratedBefore, "no confirmation became a rated answer");
    const webEdit = await sam.context.request.put(
      `${BASE}/api/conversations/${encodeURIComponent(cid)}/messages/${encodeURIComponent(target)}/feedback`,
      { headers: { Origin: BASE }, data: { expected_revision: onTarget[0].revision, rating: "negative", comment: "edited on the web" } });
    assert.equal(webEdit.status(), 200);
    await ask(p, "Save positive feedback on your previous answer");
    onTarget = await mineOn();
    assert.equal(onTarget[0].rating, "positive");
    assert.equal(await countAll(), ratedBefore);
    toolTarget = { message_id: target, rating: onTarget[0].rating, revision: onTarget[0].revision, ratings_in_conversation: ratedBefore };
    await shot(p, "13-tool-repeated-requests", "Negative, positive, negative through chat, a web edit, then positive",
      "Every request updates the one rating on the enterprise-plan answer; confirmations are never rated",
      `Target ${target}: rating ${onTarget[0].rating}, revision ${onTarget[0].revision}; ${ratedBefore} ratings in the conversation before and after`);

    // ---- Slack, local simulation -------------------------------------------------------------
    console.log("\n[slack] Signed Slack requests against the real server, fake Slack Web API");
    const channelFile = path.join(hub, "channels", "slack-main.yaml");
    fs.mkdirSync(path.dirname(channelFile), { recursive: true });
    fs.writeFileSync(channelFile, [
      "version: 1", "id: slack-main", "provider: slack", "enabled: true", "agent: main",
      "feedback: true", "provider_config:", "  transport: http", "  installation_team_id: T0E2E", "  app_id: A0E2E",
      "  bot_token_ref: env:SLACK_E2E_BOT", "  signing_secret_ref: env:SLACK_E2E_SIGNING", "  allowed_channel_ids: [C0E2E]", ""].join("\n"));
    // The channel file auto-configures this connection. Transport access is
    // an explicit account grant, separate from Sam's existing use_hub grant.
    const grantChannel = await admin.post("/portal/api/accounts/grant", {
      data: { email: SAM.email, grants: [{ hub: hubKey, permission: "channel_slack-main" }] },
    });
    assert.equal(grantChannel.status(), 200, await grantChannel.text());
    h.python(["-m", "hubzoid.cli", "channels", "link", hub, "slack-main", "--external-id", "U0SAM", "--account", SAM.email], { env });
    const hook = `${BASE}/api/hubs/${encodeURIComponent(hubKey)}/channels/slack-main/webhook`;
    const event = JSON.stringify({ type: "event_callback", api_app_id: "A0E2E", team_id: "T0E2E", event_id: "EvE2E1",
      event: { type: "app_mention", channel: "C0E2E", ts: "1800000000.000001", user: "U0SAM", text: "<@UBOT> what does the starter plan cost?" } });
    let res = await fetch(hook, { method: "POST", body: event, headers: { "content-type": "application/json", ...slackSigned(event) } });
    assert.equal(res.status, 200);
    const deadline = Date.now() + 60_000;
    let prompt = null;
    while (!prompt && Date.now() < deadline) {
      prompt = slack.calls.find((c) => c.method === "chat.postMessage" && c.args.blocks);
      if (!prompt) await new Promise((r) => setTimeout(r, 500));
    }
    assert.ok(prompt, "the rating buttons were posted after the answer");
    const promptTs = `1800000000.${String(slack.calls.filter((c) => c.method === "chat.postMessage").indexOf(prompt) + 1).padStart(6, "0")}`;
    const clickBody = (value, trigger) => "payload=" + encodeURIComponent(JSON.stringify({
      type: "block_actions", api_app_id: "A0E2E", team: { id: "T0E2E" }, trigger_id: trigger, user: { id: "U0SAM", team_id: "T0E2E" },
      container: { type: "message", message_ts: promptTs, channel_id: "C0E2E", thread_ts: "1800000000.000001" },
      actions: [{ type: "feedback_buttons", action_id: "hz_feedback", value, block_id: prompt.args.blocks[0].block_id, action_ts: `${Date.now()}` }] }));
    const bad = clickBody("up", "e2e-forged");
    res = await fetch(hook, { method: "POST", body: bad, headers: { "content-type": "application/x-www-form-urlencoded", "x-slack-request-timestamp": "1", "x-slack-signature": "v0=00" } });
    assert.equal(res.status, 401, "an unsigned click is refused");
    const down = clickBody("down", "e2e-trigger-1");
    for (let i = 0; i < 2; i++) // Slack retries: one record
      await fetch(hook, { method: "POST", body: down, headers: { "content-type": "application/x-www-form-urlencoded", ...slackSigned(down) } });
    let view = null;
    while (!view && Date.now() < deadline) {
      view = slack.calls.find((c) => c.method === "views.open");
      if (!view) await new Promise((r) => setTimeout(r, 300));
    }
    const submitBody = "payload=" + encodeURIComponent(JSON.stringify({
      type: "view_submission", api_app_id: "A0E2E", team: { id: "T0E2E" }, trigger_id: "e2e-submit", user: { id: "U0SAM", team_id: "T0E2E" },
      view: { id: "VE2E", callback_id: view.args.view.callback_id, private_metadata: view.args.view.private_metadata,
        state: { values: { reasons: { value: { selected_options: [{ value: "incorrect" }] } }, comment: { value: { value: "Slack: the starter price is out of date" } } } } } }));
    res = await fetch(hook, { method: "POST", body: submitBody, headers: { "content-type": "application/x-www-form-urlencoded", ...slackSigned(submitBody) } });
    assert.equal(await res.text(), "", "the dialog closes on an empty 200");

    // ---- the review page ---------------------------------------------------------------------
    console.log("\n[review] The agent's Feedback tab");
    const ada = await signedIn(ADMIN);
    step("Ada rates her own answer, so the review shows a comment she may read");
    await ask(ada.page, "What is the refund window?");
    await hover(ada.page);
    await last(ada.page).getByTestId("feedback-negative").click();
    await ada.page.getByTestId("feedback-form").getByTestId("feedback-comment").fill("Synthetic: the refund window changed to 30 days");
    await ada.page.getByTestId("feedback-form").getByTestId("feedback-send").click();
    await ada.page.getByTestId("feedback-form").waitFor({ state: "detached" });
    let slackRow = null;
    for (let i = 0; i < 40 && !slackRow; i++) {
      const items = (await json(ada.context, `/api/hubs/${hubKey}/feedback?source=slack`)).body.items;
      slackRow = items.find((x) => x.reason_ids.includes("incorrect"));
      if (!slackRow) await new Promise((r) => setTimeout(r, 500));
    }
    assert.ok(slackRow, "the Slack rating with its dialog reason reached the review");
    const r = ada.page;
    await r.goto(`${BASE}/portal/#/agents/${hubKey}/feedback`);
    await r.getByTestId("feedback-review").waitFor();
    await r.getByRole("radio", { name: "All time" }).click().catch(() => r.getByText("All time").click());
    await r.locator(".ant-table-row").first().waitFor();
    const list = (await json(ada.context, `/api/hubs/${hubKey}/feedback`)).body;
    assert.equal(await r.locator(".ant-table-row").count(), list.items.length);
    const samRow = list.items.find((x) => x.author === SAM.name && x.source === "web" && x.rating === "negative");
    assert.ok(samRow.comment_hidden && samRow.comment === null && samRow.excerpt === null, "a manager without the conversation sees no comment or answer");
    await shot(r, "05-agent-feedback-page", "Settings, the agent, Feedback", "Every rating with source, author and date",
      `${list.items.length} ratings; ${list.counts.positive} helpful, ${list.counts.negative} not helpful; Sam's comment hidden from Ada`);
    step("Filters: Not helpful from the web chat");
    await r.getByText("Not helpful", { exact: true }).first().click();
    await r.locator(".ant-table-row").first().waitFor();
    await shot(r, "06-filtered-feedback", "Rating filter", "Only not-helpful ratings", "Filtered list");
    await shot(r, "12a-slack-local-simulation", "Slack rating (local simulation, fake Slack API, signed requests)",
      "A Slack row with the dialog's reason and comment, attributed to the linked account",
      `Slack row by ${slackRow.author}: ${slackRow.reason_ids.join(", ")}`, "local simulation");
    step("API and export agree with the page");
    const exported = await (await ada.context.request.get(`${BASE}/api/hubs/${hubKey}/feedback/export?format=json`)).json();
    assert.deepEqual(exported.items.map((x) => x.id).sort(), list.items.map((x) => x.id).sort());
    const csv = await (await ada.context.request.get(`${BASE}/api/hubs/${hubKey}/feedback/export?format=csv`)).text();
    assert.ok(!csv.includes(COMMENT), "Sam's comment is not exported to a reviewer who cannot open it");
    step("Open the source: Ada's own conversation");
    await r.getByText("All", { exact: true }).first().click();
    await r.getByRole("link", { name: "Conversation" }).first().click();
    await r.waitForURL(/\/c\//);
    await r.getByText("refund window").first().waitFor();
    await shot(r, "07-source-context", "Open a rating's conversation", "The rated answer in its conversation", "Opened Ada's own conversation");

    step("Sam, an ordinary member, cannot review feedback");
    assert.equal((await json(sam.context, `/api/hubs/${hubKey}/feedback`)).status, 403);
    assert.equal((await json(sam.context, "/api/chat/feedback/export?format=csv")).status, 403);
    await p.goto(`${BASE}/portal/#/agents/${hubKey}/feedback`);
    await p.waitForTimeout(2000);
    assert.equal(await p.getByTestId("feedback-review").count(), 0);
    await shot(p, "08-access-denied", "A member opens the Feedback page", "No ratings shown; API 403", "Refused; API 403");

    // ---- disabled collection -----------------------------------------------------------------
    step("Feedback off: a page open before the change gets a clear refusal; after a reload no controls");
    await p.goto(`${BASE}/c/${encodeURIComponent(cid)}`);
    await composer(p).waitFor();
    await setFeedback({ enabled: false });
    await hover(p);
    await last(p).getByTestId("feedback-positive").click();
    await p.getByText("Feedback is turned off here").first().waitFor();
    await shot(p, "10c-feedback-off", "Feedback turned off while the page was open", "Refused with a plain sentence",
      "Toast: feedback is turned off; nothing saved");
    await p.reload();
    await composer(p).waitFor();
    await hover(p);
    assert.equal(await p.getByTestId("feedback-positive").count(), 0);
    await setFeedback({ enabled: true });

    // ---- narrow, light, dark ----------------------------------------------------------------
    for (const scheme of ["light", "dark"]) {
      step(`Phone width, ${scheme}: the form fits without sideways scrolling`);
      const phone = await signedIn(SAM, { viewport: { width: 390, height: 844 }, colorScheme: scheme, hasTouch: true });
      await phone.page.goto(`${BASE}/c/${encodeURIComponent(cid)}`);
      await composer(phone.page).waitFor();
      await last(phone.page).tap().catch(() => last(phone.page).click());
      await last(phone.page).getByTestId("feedback-negative").click();
      await phone.page.getByTestId("feedback-form").waitFor();
      const [sw, w] = await phone.page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
      assert.ok(sw <= w, `no sideways scroll (${sw} > ${w})`);
      await shot(phone.page, `09-narrow-${scheme}-form`, `Phone width, ${scheme} theme`, "The form fits the screen",
        `${w}px wide, no sideways scroll`);
      await phone.context.close();
    }
    for (const scheme of ["light", "dark"]) {
      const wide = await signedIn(ADMIN, { colorScheme: scheme });
      await wide.page.goto(`${BASE}/portal/#/agents/${hubKey}/feedback`);
      await wide.page.getByTestId("feedback-review").waitFor();
      await shot(wide.page, `10-review-${scheme}`, `Feedback page, ${scheme} theme`, "Readable in both themes", "Rendered");
      await wide.context.close();
    }
    {
      const narrow = await signedIn(ADMIN, { viewport: { width: 390, height: 844 } });
      await narrow.page.goto(`${BASE}/portal/#/agents/${hubKey}/feedback`);
      await narrow.page.getByTestId("feedback-review").waitFor();
      await shot(narrow.page, "09b-narrow-review", "Feedback page at phone width", "The table scrolls inside its frame", "Rendered");
      await narrow.context.close();
    }
    await sam.context.close();
    await ada.context.close();

    // ---- restart, WhatsApp (local simulation) and another hub's rating ---------------------
    console.log("\n[restart] Stop the server, simulate WhatsApp, add another hub's rating, start again");
    await server.stop();
    const sim = JSON.parse(h.python([path.join(__dirname, "feedback_whatsapp_sim.py"), hub, ADMIN.email]).trim().split("\n").at(-1));
    assert.equal(sim.sent, 1, "one approved reply went to the fake Meta API");
    assert.match(sim.footer, /react 👍 or 👎/);
    h.python(["-c", [
      "import sys, time", "from pathlib import Path", "from hubzoid import db", "from hubzoid.chat import feedback as fb",
      "e = db.operational_engine(Path(sys.argv[1]))",
      "with e.begin() as c: c.execute(fb.feedback.insert().values(id='other-hub-1', hub_id='second-desk', conversation_id=None, message_id='x-1', author_id='u-x', rating='negative', reason_ids='[]', comment='another hub', tags='[]', revision=1, created_at=time.time(), updated_at=time.time(), source='web'))",
    ].join("\n"), hub]);
    server = new h.Server("feedback", hub, port, "127.0.0.1", env);
    server.start();
    await server.ready();
    const again = await signedIn(ADMIN);
    const after2 = (await json(again.context, `/api/hubs/${hubKey}/feedback`)).body;
    assert.ok(!after2.items.some((x) => x.id === "other-hub-1"), "another hub's rating never shows here");
    assert.equal((await json(again.context, "/api/hubs/second-desk/feedback")).status, 404);
    const wa = after2.items.find((x) => x.source === "whatsapp");
    assert.ok(wa && wa.author_kind === "external" && wa.comment === "Friday is too late, we need it Wednesday");
    assert.ok(after2.items.some((x) => x.source === "slack") && after2.items.some((x) => x.source === "tool"));
    await again.page.goto(`${BASE}/portal/#/agents/${hubKey}/feedback`);
    await again.page.getByTestId("feedback-review").waitFor();
    await again.page.getByText("All time").click();
    await again.page.locator(".ant-table-row").first().waitFor();
    await shot(again.page, "11-tool-feedback-in-review", "After a restart: every source", "Web, tool, Slack and WhatsApp ratings kept",
      `${after2.items.length} ratings after restart; tool rating targets the previous answer`);
    await again.page.getByRole("combobox", { name: "Source" }).click();
    await again.page.getByTitle("WhatsApp").click();
    await again.page.locator(".ant-table-row").first().waitFor();
    await shot(again.page, "12b-whatsapp-local-simulation", "WhatsApp rating (local simulation, signed webhook, fake Meta API)",
      "An outside sender's rating, masked, never an account; the approved reply as the answer",
      `${wa.author}: ${wa.comment}`, "local simulation");
    await again.context.close();
    const samAgain = await signedIn(SAM);
    await samAgain.page.goto(`${BASE}/c/${encodeURIComponent(cid)}`);
    await composer(samAgain.page).waitFor();
    assert.ok((await json(samAgain.context, `/api/conversations/${encodeURIComponent(cid)}/feedback`)).body.items.length >= 2);
    await samAgain.context.close();

    assert.deepEqual(errors, [], "no console or page errors");
    console.log(`\nPASS: ${steps.length} checks, ${shots.length} screenshots in ${SHOTS}`);
  } catch (error) {
    console.error(`\nFAIL at: ${steps[steps.length - 1]}\n`, error);
    if (errors.length) console.error("console errors:", errors);
    for (const context of browser?.contexts() ?? [])
      for (const page of context.pages()) await page.screenshot({ path: path.join(SHOTS, "failure.png") }).catch(() => {});
    if (server) console.error(`server log: ${server.log}`);
    process.exitCode = 1;
  } finally {
    fs.writeFileSync(path.join(WORK, "feedback-journey.json"), JSON.stringify({ steps, shots, errors, toolTarget }, null, 2));
    await browser?.close().catch(() => {});
    await server?.stop();
    slack?.server.close();
  }
}

main();
