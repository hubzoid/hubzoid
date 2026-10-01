// Browser journeys for the Hubzoid chat app (/, /c/:id, /s/:id, /auth,
// /account). By default they run the built bundle (hubzoid/portal_dist) against
// the in-memory fixture in app-fixture-server.cjs: synthetic people, no model,
// no live data. Set BASE_URL to run the journeys that don't depend on the
// fixture's scripted replies against a real Hubzoid server instead:
//
//   npm run build && node tests/app-journey.cjs
//   BASE_URL=http://127.0.0.1:8080 HZ_EMAIL=you@example.com HZ_PASSWORD=... node tests/app-journey.cjs
//
// Screenshots go to APP_SHOTS (default: <tmp>/hubzoid-app-tests).
const { chromium } = require("playwright");
const { AxeBuilder } = require("@axe-core/playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const zlib = require("node:zlib");

const REAL = process.env.BASE_URL || "";
const shots = process.env.APP_SHOTS || path.join(os.tmpdir(), "hubzoid-app-tests");
fs.mkdirSync(shots, { recursive: true });
const ID = /^[A-Za-z0-9_-]{8,64}$/;

const steps = [];
const skipped = [];
function step(name) {
  steps.push(name);
  console.log(`· ${name}`);
}
// Dialogs and drawers animate in; let them land before a screenshot.
const shot = async (page, name) => {
  await page.waitForTimeout(260);
  await page.screenshot({ path: path.join(shots, `${name}.png`) });
};

/** A small bar chart as a PNG, so the upload preview has something to show. */
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
  header[8] = 8; // bit depth
  header[9] = 2; // truecolour
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

(async () => {
  let fixture = null;
  let local = null;
  let BASE = REAL.replace(/\/+$/, "");
  let LOCAL_BASE = "";
  if (!REAL) {
    const { createApp } = require("./app-fixture-server.cjs");
    fixture = createApp({ mode: "accounts" });
    local = createApp({ mode: "local" });
    const port = Number(process.env.APP_PORT || 3421);
    BASE = `http://127.0.0.1:${await fixture.listen(port)}`;
    LOCAL_BASE = `http://127.0.0.1:${await local.listen(port + 1)}`;
  }
  const EMAIL = process.env.HZ_EMAIL || "ada@example.com";
  const PASSWORD = process.env.HZ_PASSWORD || "hubzoid-demo";
  const onlyFixture = (name, fn) => {
    if (fixture) return fn();
    skipped.push(name);
    console.log(`- skipped (needs the fixture): ${name}`);
  };
  const requests = () => fixture.state.requests;
  const since = (mark) => requests().slice(mark);

  const browser = await chromium.launch({ headless: true });
  let diagnosticPage;
  const errors = [];
  const watch = (page) => {
    page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
    page.on("console", (m) => {
      // Non-2xx fetches log "Failed to load resource"; the negative journeys
      // provoke 401/403/404/503 on purpose and the app handles them in the UI.
      if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text());
    });
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

  try {
    const context = await browser.newContext({ viewport: { width: 1360, height: 900 } });
    const page = await context.newPage();
    diagnosticPage = page;
    watch(page);
    const composer = () => page.getByRole("textbox", { name: /^Message / });
    const send = async (text) => {
      await composer().fill(text);
      await composer().press("Enter");
    };
    const lastAssistant = () => page.locator('[data-role="assistant"]').last();
    const waitIdle = async () => {
      await page.getByTestId("send").waitFor();
    };
    // The composer takes focus when a chat page mounts; wait for that before
    // opening menus, which close when focus moves.
    const settled = async () => {
      await composer().waitFor();
      await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label")?.startsWith("Message "));
    };
    const signIn = async (p, email = EMAIL, password = PASSWORD) => {
      await p.getByLabel("Email").fill(email);
      await p.getByLabel("Password").fill(password);
      await p.getByRole("button", { name: "Sign in", exact: true }).click();
    };

    // ---- sign-in -----------------------------------------------------------------
    step("A signed-out visitor goes to sign-in, keeping the page to come back to");
    await page.goto(`${BASE}/account/connections`);
    await page.waitForURL(/\/auth\?redirect=%2Faccount%2Fconnections/);
    await page.getByRole("heading", { name: /^Sign in to / }).waitFor();
    assert.match(await page.title(), /Sign in/);
    await shot(page, "app-01-sign-in");
    await axe(page, "sign-in");

    await onlyFixture("sign-in errors", async () => {
      step("Every sign-in failure reads as a plain sentence");
      await page.getByRole("button", { name: "Sign in", exact: true }).click();
      await page.getByText("Enter your email address.").waitFor();
      await page.getByText("Enter your password.").waitFor();
      await signIn(page, "ada@example.com", "wrong-password");
      await page.getByText("That email and password don't match. Check them and try again.").waitFor();
      await signIn(page, "pending@example.com");
      await page.getByText(/waiting for an administrator to approve it/).waitFor();
      await signIn(page, "suspended@example.com");
      await page.getByText(/This account is suspended/).waitFor();
      await signIn(page, "limited@example.com");
      await page.getByText("Too many attempts. Try again in 15 minutes.").waitFor();
      await page.goto(`${BASE}/auth?error=domain_not_allowed`);
      await page.getByText("Accounts from that email domain can't sign in here. Use your work account.").waitFor();
      await page.goto(`${BASE}/auth?error=something_new`);
      await page.getByText("Sign-in didn't complete. Try again, or use another way to sign in.").waitFor();
      await page.goto(`${BASE}/auth?redirect=%2Faccount%2Fconnections`);
    });

    step("Signing in returns to the page that asked for it");
    await signIn(page);
    await page.waitForURL(`${BASE}/account/connections`);
    await page.getByRole("heading", { name: "Connections", level: 1 }).waitFor();

    // ---- new chat --------------------------------------------------------------------
    step("A new chat offers agent cards; ?agent= and the Console's ?models= preselect one");
    await page.goto(`${BASE}/`);
    await page.getByRole("heading", { name: /^What can .+ help with\?$/ }).waitFor();
    await onlyFixture("agent picker", async () => {
      const cards = page.getByRole("radiogroup", { name: "Choose an agent" }).getByRole("radio");
      assert.equal(await cards.count(), 3);
      await page.goto(`${BASE}/?agent=finance`);
      await page.getByRole("radio", { name: /Finance Assistant/ }).and(page.locator('[aria-checked="true"]')).waitFor();
      await page.getByRole("heading", { name: "What can Finance Assistant help with?" }).waitFor();
      await page.goto(`${BASE}/?models=it-ops`);
      await page.getByRole("heading", { name: "What can IT Ops help with?" }).waitFor();
      await page.goto(`${BASE}/?agent=nobody`);
      await page.getByText(/isn't available to you anymore/).waitFor();
      await page.getByRole("radio", { name: /Hubzoid Guide/ }).click();
      await page.getByRole("heading", { name: "What can Hubzoid Guide help with?" }).waitFor();
      await page.getByRole("button", { name: "What is Hubzoid?" }).waitFor();
    });
    await shot(page, "app-02-new-chat");
    await axe(page, "new chat");

    step("A message streams in, the chat gets its own URL and a title in the sidebar");
    const mark = fixture ? requests().length : 0;
    await send("Hello there, what is a hub?");
    await page.getByRole("button", { name: "Stop response" }).waitFor();
    await page.waitForURL(/\/c\/[^/]+$/);
    await waitIdle();
    let firstId = decodeURIComponent(page.url().split("/c/")[1]);
    assert.match(firstId, ID);
    await lastAssistant().getByText(/covered in the hub's knowledge|./).first().waitFor();
    await onlyFixture("chat request body", async () => {
      const chat = since(mark).find((r) => r.path === "/api/chat");
      assert.ok(chat, "POST /api/chat was sent");
      const conv = fixture.state.conversations.get(firstId);
      assert.ok(conv, "the conversation exists on the server");
      const msgs = [...fixture.state.messages.values()].filter((m) => m.conversation_id === firstId);
      assert.equal(msgs.length, 2);
      const [user, reply] = msgs.sort((a, b) => a.created_at - b.created_at);
      assert.match(user.id, ID);
      assert.match(reply.id, ID);
      assert.equal(user.parent_id, null);
      assert.equal(reply.parent_id, user.id);
      assert.equal(conv.head_id, reply.id);
      const sidebar = page.getByRole("navigation", { name: "Conversations" });
      await sidebar.getByRole("link", { name: "Hello there what is a" }).waitFor();
      await page.getByTestId("chat-title").getByText("Hello there what is a").waitFor();
      await page.getByTestId("live-region").getByText("Response complete").waitFor();
    });
    await shot(page, "app-03-first-reply");

    await onlyFixture("scripted replies", async () => {
      step("Tool calls are collapsible entries that show running, done and failed");
      await send("Please use a tool to check pricing");
      const running = page.getByTestId("tool-entry").last();
      await running.getByText("Running").waitFor();
      await running.getByText("Done").waitFor();
      await waitIdle();
      const toggle = running.getByRole("button", { name: /read_knowledge/ });
      assert.equal(await toggle.getAttribute("aria-expanded"), "false");
      await toggle.click();
      assert.equal(await toggle.getAttribute("aria-expanded"), "true");
      await running.getByText('"path": "knowledge/pricing.md"').waitFor();
      await send("try the failing tool");
      const failed = page.getByTestId("tool-entry").last();
      await failed.getByText("Failed").waitFor();
      await failed.getByText("The CRM didn't respond in time.").first().waitFor();
      await waitIdle();
      await shot(page, "app-04-tools");

      step("Reasoning is collapsed by default, and an indicator alone reads Thinking…");
      await send("Can you think about which plan is better?");
      await waitIdle();
      const thought = page.getByRole("button", { name: /Thought process/ }).last();
      assert.equal(await thought.getAttribute("aria-expanded"), "false");
      await thought.click();
      await page.getByText("The person wants a comparison.", { exact: false }).waitFor();
      await send("answer with the indicator only");
      await page.getByTestId("reasoning").last().getByText("Thinking…").waitFor();
      await waitIdle();
      await shot(page, "app-05-reasoning");

      step("Markdown tables and highlighted code, with a copy button; files the agent wrote are download links");
      await send("Show me a table and code");
      await waitIdle();
      await lastAssistant().getByRole("table").waitFor();
      await lastAssistant().getByRole("columnheader", { name: "Seats" }).waitFor();
      await lastAssistant().locator(".shiki").first().waitFor();
      await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE });
      await lastAssistant().getByRole("button", { name: "Copy code" }).first().click();
      await lastAssistant().getByRole("button", { name: "Copied" }).first().waitFor();
      assert.match(await page.evaluate(() => navigator.clipboard.readText()), /import csv/);
      await shot(page, "app-06-markdown");
      await send("write the report for download");
      await waitIdle();
      const link = lastAssistant().getByRole("link", { name: "Download quarterly-report.csv" });
      assert.equal(await link.getAttribute("href"), `/artifacts/${firstId}/quarterly-report.csv`);

      step("A run error reads as a sentence and offers Try again");
      await send("cause an error please");
      await waitIdle();
      await lastAssistant().getByText("The agent couldn't finish this reply: The model provider is unavailable right now.").waitFor();
      await lastAssistant().getByRole("button", { name: "Try again" }).waitFor();

      step("Stop ends the reply here and cancels the run on the server");
      const before = requests().length;
      await send("give me a slow answer");
      await lastAssistant().getByText("Step 3 of the long answer.", { exact: false }).waitFor();
      await page.getByRole("button", { name: "Stop response" }).click();
      await lastAssistant().getByTestId("stopped").waitFor();
      await page.waitForFunction(() => true);
      const cancel = since(before).find((r) => /\/api\/runs\/[^/]+\/cancel$/.test(r.path));
      assert.ok(cancel, "the cancel endpoint was called");
      const stoppedId = cancel.path.split("/")[3];
      await page.waitForTimeout(400);
      assert.equal(fixture.state.messages.get(stoppedId).status, "cancelled");
      await page.getByTestId("live-region").getByText("Response stopped").waitFor();
      await shot(page, "app-07-stopped");

      step("Edit makes a new version, regenerate makes another, and the branch picker switches between them");
      await page.goto(`${BASE}/`);
      await send("Tell me about hubs");
      await page.waitForURL(/\/c\//);
      await waitIdle();
      const convId = decodeURIComponent(page.url().split("/c/")[1]);
      const userMsg = page.getByRole("article", { name: "You said" }).first();
      await userMsg.hover();
      await userMsg.getByRole("button", { name: "Edit message" }).click();
      const edit = page.getByRole("textbox", { name: "Edit message" });
      await edit.fill("Tell me about agents instead");
      await shot(page, "app-08-editing");
      await edit.press("Enter");
      await waitIdle();
      await lastAssistant().getByText("Tell me about agents instead", { exact: false }).waitFor();
      assert.deepEqual(await page.getByTestId("branch-count").allTextContents(), ["2 / 2"]);
      await lastAssistant().getByRole("button", { name: "Regenerate response" }).click();
      await waitIdle();
      assert.deepEqual(await page.getByTestId("branch-count").allTextContents(), ["2 / 2", "2 / 2"]);
      const regen = [...fixture.state.messages.values()].filter((m) => m.conversation_id === convId);
      assert.equal(regen.length, 5, "two user versions and three replies");
      const headMark = requests().length;
      await page.getByRole("button", { name: "Previous version" }).first().click();
      await page.getByTestId("user-text").getByText("Tell me about hubs").waitFor();
      assert.deepEqual(await page.getByTestId("branch-count").allTextContents(), ["1 / 2"]);
      await page.waitForFunction(() => true);
      await page.waitForTimeout(500);
      assert.ok(since(headMark).some((r) => r.method === "PATCH" && r.path === `/api/conversations/${convId}`), "head_id saved");
      const firstReply = [...fixture.state.messages.values()].find((m) => m.conversation_id === convId && m.role === "assistant" && m.created_at === Math.min(...regen.filter((x) => x.role === "assistant").map((x) => x.created_at)));
      assert.equal(fixture.state.conversations.get(convId).head_id, firstReply.id);
      await page.reload();
      await page.getByTestId("user-text").getByText("Tell me about hubs").waitFor();
      await shot(page, "app-09-branches");

      step("A reply still running after a reload shows progress and finishes on its own");
      await send("a long answer please");
      await lastAssistant().getByText("Part 2.", { exact: false }).waitFor();
      await page.reload();
      await page.getByRole("button", { name: "Stop response" }).waitFor();
      await lastAssistant().getByText("That's everything.").waitFor({ timeout: 15000 });
      await waitIdle();
      assert.equal(await page.getByTestId("streaming").count(), 0);

      step("Files and images upload at once with progress, preview, and go with the message");
      const uploadMark = requests().length;
      const chooser = page.waitForEvent("filechooser");
      await page.getByRole("button", { name: "Attach files" }).click();
      const png = chartPng();
      await (await chooser).setFiles([
        { name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("Quarterly notes\n") },
        { name: "chart.png", mimeType: "image/png", buffer: png },
      ]);
      const chips = page.getByTestId("composer-attachment");
      await chips.first().waitFor();
      assert.equal(await chips.count(), 2);
      await page.getByTestId("composer").getByRole("img", { name: "Preview of chart.png" }).waitFor();
      await page.waitForFunction(() => [...document.querySelectorAll('[data-testid="composer-attachment"]')].every((el) => el.dataset.status === "requires-action"));
      assert.equal(since(uploadMark).filter((r) => r.method === "POST" && r.path.endsWith("/files")).length, 2, "uploaded right away");
      await shot(page, "app-10-attachments");
      await send("What do these files show?");
      await waitIdle();
      const sent = [...fixture.state.messages.values()].filter((m) => m.conversation_id === convId && m.role === "user").pop();
      assert.deepEqual(sent.content.filter((p) => p.type !== "text").map((p) => [p.type, p.name]), [["file", "notes.txt"], ["image", "chart.png"]]);
      await page.getByRole("link", { name: "Preview of chart.png" }).waitFor();
      await lastAssistant().getByText("I received 2 files", { exact: false }).waitFor();
      await shot(page, "app-10b-attachments-sent");

      step("An attachment that's too big is refused in plain words");
      const big = page.waitForEvent("filechooser");
      await page.getByRole("button", { name: "Attach files" }).click();
      await (await big).setFiles([{ name: "huge.bin", mimeType: "application/octet-stream", buffer: Buffer.alloc(26 * 1024 * 1024) }]);
      await page.getByText("huge.bin is larger than 25 MB, the limit for one file.").waitFor();

      step("Files can also be dropped on the chat or pasted into the composer, and more than ten are refused");
      const dropped = await page.evaluateHandle(() => {
        const dt = new DataTransfer();
        dt.items.add(new File(["# Dropped notes\n"], "dropped.md", { type: "text/markdown" }));
        return dt;
      });
      const zone = page.getByTestId("thread-viewport");
      await zone.dispatchEvent("dragenter", { dataTransfer: dropped });
      await page.getByText("Drop files to attach them").waitFor();
      await shot(page, "app-10c-drop-target");
      await zone.dispatchEvent("drop", { dataTransfer: dropped });
      await chips.filter({ hasText: "dropped.md" }).waitFor();
      await composer().focus();
      await page.evaluate(() => {
        const dt = new DataTransfer();
        dt.items.add(new File(["pasted"], "pasted.txt", { type: "text/plain" }));
        document.activeElement.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
      });
      await chips.filter({ hasText: "pasted.txt" }).waitFor();
      const many = page.waitForEvent("filechooser");
      await page.getByRole("button", { name: "Attach files" }).click();
      await (await many).setFiles(
        Array.from({ length: 9 }, (_, i) => ({ name: `part-${i + 1}.txt`, mimeType: "text/plain", buffer: Buffer.from(`part ${i + 1}`) })),
      );
      await page.getByText("You can attach up to 10 files to one message.").waitFor();
      await page.waitForFunction(() => document.querySelectorAll('[data-testid="composer-attachment"]').length === 10);
      await page.waitForTimeout(300);
      assert.equal(await chips.count(), 10);
      while ((await chips.count()) > 0) {
        await chips.first().getByRole("button", { name: /^Remove / }).click();
        await page.waitForTimeout(50);
      }

      step("Hub-scoped calls go to the agent's own bridge (api_base), everything else to the deployment");
      const hubMark = requests().length;
      await page.goto(`${BASE}/?agent=finance`);
      await send("Summarise the close");
      await page.waitForURL(/\/c\//);
      await waitIdle();
      const scoped = since(hubMark).map((r) => `${r.method} ${r.path.split("?")[0]}`);
      assert.ok(scoped.includes("POST /b/finance/api/conversations"), scoped.join(", "));
      assert.ok(scoped.includes("POST /b/finance/api/chat"), scoped.join(", "));
      assert.ok(!scoped.some((r) => r.startsWith("GET /b/")), "list and detail calls take no hub prefix");
    });

    // ---- sidebar ---------------------------------------------------------------------
    const sidebar = page.getByRole("navigation", { name: "Conversations" });
    if (!fixture) {
      // Against a real server, only touch the chat this run created.
      step("Rename, search, share, archive, restore and delete the chat this run created");
      const row = sidebar.locator(`li:has(a[href="/c/${encodeURIComponent(firstId)}"])`);
      await row.waitFor();
      const stamp = `Journey check ${Date.now().toString(36)}`;
      await row.getByRole("button", { name: /^Options for / }).click();
      await page.getByRole("menuitem", { name: "Rename" }).click();
      await sidebar.getByRole("textbox", { name: "Chat title" }).fill(stamp);
      await sidebar.getByRole("textbox", { name: "Chat title" }).press("Enter");
      await sidebar.getByRole("link", { name: stamp }).waitFor();
      await sidebar.getByRole("searchbox", { name: "Search chats" }).fill(stamp);
      await sidebar.getByRole("link", { name: stamp }).waitFor();
      await sidebar.getByRole("button", { name: "Clear search" }).click();
      await page.goto(`${BASE}/c/${encodeURIComponent(firstId)}`);
      await settled();
      await page.getByRole("button", { name: "Share", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: "Share chat" });
      await dialog.getByRole("button", { name: "Create link" }).click();
      const link = await dialog.getByRole("textbox", { name: "Share link" }).inputValue();
      const viewer = await context.newPage();
      watch(viewer);
      await viewer.goto(link);
      await viewer.getByRole("heading", { name: stamp, level: 1 }).waitFor();
      await viewer.close();
      await dialog.getByRole("button", { name: "Turn off link" }).click();
      await page.keyboard.press("Escape");
      await sidebar.getByRole("link", { name: stamp }).waitFor();
      const again = sidebar.locator(`li:has(a[href="/c/${encodeURIComponent(firstId)}"])`);
      await again.getByRole("button", { name: /^Options for / }).click();
      await page.getByRole("menuitem", { name: "Archive" }).click();
      await sidebar.getByRole("link", { name: stamp }).waitFor({ state: "detached" });
      await sidebar.getByRole("button", { name: "Archived chats" }).click();
      await sidebar.getByRole("link", { name: stamp }).waitFor();
      await sidebar.locator(`li:has(a[href="/c/${encodeURIComponent(firstId)}"])`).getByRole("button", { name: /^Options for / }).click();
      await page.getByRole("menuitem", { name: "Restore" }).click();
      await sidebar.getByRole("button", { name: "Back to chats" }).click();
      await sidebar.locator(`li:has(a[href="/c/${encodeURIComponent(firstId)}"])`).getByRole("button", { name: /^Options for / }).click();
      await page.getByRole("menuitem", { name: "Delete" }).click();
      await page.getByRole("alertdialog").getByRole("button", { name: "Delete chat" }).click();
      await sidebar.getByRole("link", { name: stamp }).waitFor({ state: "detached" });
      // Later steps need a chat: start a fresh one.
      await page.goto(`${BASE}/`);
      await settled();
      await send("Hello again");
      await page.waitForURL(/\/c\//);
      await waitIdle();
      firstId = decodeURIComponent(page.url().split("/c/")[1]);
    }
    await onlyFixture("sidebar", async () => {
      step("Conversations are grouped by date, rename inline, and search as you type");
      await page.goto(`${BASE}/`);
      await settled();
      for (const group of ["Today", "Yesterday", "Previous 7 days", "Older"]) await sidebar.getByRole("heading", { name: group }).waitFor();
      await sidebar.getByRole("button", { name: "Options for Standup notes agent" }).click();
      await page.getByRole("menuitem", { name: "Rename" }).click();
      const field = sidebar.getByRole("textbox", { name: "Chat title" });
      await field.fill("Standup agent plan");
      await field.press("Enter");
      await sidebar.getByRole("link", { name: "Standup agent plan" }).waitFor();
      assert.equal(fixture.state.conversations.get("c_adastandup01").title, "Standup agent plan");
      await sidebar.getByRole("searchbox", { name: "Search chats" }).fill("variance");
      await sidebar.getByRole("link", { name: "Budget variance for Q3" }).waitFor();
      await page.waitForFunction(() => document.querySelectorAll('nav[aria-label="Conversations"] li a').length === 1);
      assert.ok(requests().some((r) => r.path.includes("q=variance")), "search went to the server");
      await sidebar.getByRole("button", { name: "Clear search" }).click();
      await sidebar.getByRole("link", { name: "VPN runbook" }).waitFor();

      step("Archive hides a chat, the Archived view shows it, and it can be restored");
      await sidebar.getByRole("button", { name: "Options for VPN runbook" }).click();
      await page.getByRole("menuitem", { name: "Archive" }).click();
      await page.getByText("Chat archived.").waitFor();
      await sidebar.getByRole("link", { name: "VPN runbook" }).waitFor({ state: "detached" });
      await sidebar.getByRole("button", { name: "Archived chats" }).click();
      await sidebar.getByRole("heading", { name: "Archived" }).waitFor();
      await sidebar.getByRole("link", { name: "VPN runbook" }).waitFor();
      await sidebar.getByRole("link", { name: "Old onboarding checklist" }).waitFor();
      await shot(page, "app-11-archived");
      await sidebar.getByRole("button", { name: "Options for VPN runbook" }).click();
      await page.getByRole("menuitem", { name: "Restore" }).click();
      await sidebar.getByRole("link", { name: "VPN runbook" }).waitFor({ state: "detached" });
      await sidebar.getByRole("button", { name: "Back to chats" }).click();
      await sidebar.getByRole("link", { name: "VPN runbook" }).waitFor();

      step("Delete asks first, then removes the chat and its messages");
      await sidebar.getByRole("button", { name: "Options for VPN runbook" }).click();
      await page.getByRole("menuitem", { name: "Delete" }).click();
      const dialog = page.getByRole("alertdialog", { name: "Delete this chat?" });
      await dialog.waitFor();
      await shot(page, "app-12-delete-confirm");
      await dialog.getByRole("button", { name: "Cancel" }).click();
      await dialog.waitFor({ state: "detached" });
      await sidebar.getByRole("button", { name: "Options for VPN runbook" }).click();
      await page.getByRole("menuitem", { name: "Delete" }).click();
      await page.getByRole("alertdialog").getByRole("button", { name: "Delete chat" }).click();
      await sidebar.getByRole("link", { name: "VPN runbook" }).waitFor({ state: "detached" });
      assert.equal(fixture.state.conversations.has("c_adavpnbook1"), false);
      assert.ok(requests().some((r) => r.method === "DELETE" && r.path === "/b/it-ops/api/conversations/c_adavpnbook1"), "delete is hub-scoped");
    });

    // ---- share -----------------------------------------------------------------------
    await onlyFixture("share", async () => {
      step("Share makes a read-only link that another person can open, until it's turned off");
      await page.goto(`${BASE}/c/c_adapricing01`);
      await settled();
      await page.getByRole("button", { name: "Share", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: "Share chat" });
      await dialog.getByRole("button", { name: "Create link" }).click();
      const urlField = dialog.getByRole("textbox", { name: "Share link" });
      await urlField.waitFor();
      const shareUrl = await urlField.inputValue();
      assert.match(shareUrl, new RegExp(`^${BASE}/s/[A-Za-z0-9_-]+$`));
      await page.getByText("Link copied.").waitFor();
      await shot(page, "app-13-share-dialog");
      await axe(page, "share dialog");

      const other = await browser.newContext({ viewport: { width: 1200, height: 860 } });
      const guest = await other.newPage();
      watch(guest);
      await guest.goto(shareUrl);
      await guest.waitForURL(/\/auth\?redirect=%2Fs%2F/);
      await signIn(guest, "sam@example.com");
      await guest.waitForURL(shareUrl);
      await guest.getByRole("heading", { name: "Pricing page questions", level: 1 }).waitFor();
      await guest.getByTestId("share-byline").getByText(/^Shared by Ada Okafor · /).waitFor();
      await guest.getByText("covers up to 25 people", { exact: false }).waitFor();
      await guest.getByTestId("tool-entry").getByText("read_knowledge").waitFor();
      assert.equal(await guest.getByRole("textbox", { name: /^Message / }).count(), 0, "read-only: no composer");
      assert.equal(await guest.getByRole("button", { name: "Edit message" }).count(), 0);
      await shot(guest, "app-14-shared-view");
      await axe(guest, "shared view");

      await page.keyboard.press("Escape");
      await page.getByRole("button", { name: "Share", exact: true }).click();
      await dialog.getByRole("button", { name: "Turn off link" }).click();
      await page.getByText("Link turned off. It no longer opens this chat.").waitFor();
      await page.keyboard.press("Escape");
      await dialog.waitFor({ state: "detached" });
      await guest.reload();
      await guest.getByRole("heading", { name: "Link not available" }).waitFor();
      await other.close();
    });

    // ---- account ---------------------------------------------------------------------
    step("Account: change the display name and the password");
    await page.goto(`${BASE}/account`);
    await page.getByRole("heading", { name: "Account", level: 1 }).waitFor();
    await axe(page, "account");
    await onlyFixture("account changes", async () => {
      await page.getByLabel("Display name").fill("Ada O.");
      await page.getByRole("button", { name: "Save", exact: true }).click();
      await page.getByText("Name saved.").waitFor();
      await page.getByRole("navigation", { name: "Conversations" }).getByText("Ada O.").waitFor();
      await page.getByLabel("Current password").fill("not-my-password");
      await page.getByLabel("New password", { exact: true }).fill("a-new-password-1");
      await page.getByLabel("Confirm new password").fill("a-new-password-1");
      await page.getByRole("button", { name: "Change password" }).click();
      await page.getByText("Your current password isn't right.").waitFor();
      await page.getByLabel("Current password").fill(PASSWORD);
      await page.getByRole("button", { name: "Change password" }).click();
      await page.getByText("Password changed. Your other sessions were signed out.").waitFor();
      assert.equal(fixture.state.users.find((u) => u.email === "ada@example.com").password, "a-new-password-1");
      fixture.state.users.find((u) => u.email === "ada@example.com").password = PASSWORD;
      await shot(page, "app-15-account");
    });

    // ---- connections -----------------------------------------------------------------
    step("Connections: status badges, connect through the provider, disconnect");
    await page.goto(`${BASE}/account/connections`);
    await page.getByRole("heading", { name: "Connections", level: 1 }).waitFor();
    await onlyFixture("connections", async () => {
      const github = page.getByTestId("connection-github");
      await github.getByText("Not connected").waitFor();
      await page.getByTestId("connection-notion").getByText("Connected", { exact: true }).waitFor();
      await page.getByTestId("connection-salesforce").getByText("Not available to you").waitFor();
      await shot(page, "app-16-connections");
      await axe(page, "connections");
      await github.getByRole("button", { name: "Connect GitHub" }).click();
      await page.waitForURL(/__fixture\/provider\/github/);
      await page.getByRole("link", { name: "Allow" }).click();
      await page.waitForURL(`${BASE}/account/connections`);
      await page.getByText("Connected to GitHub.").waitFor();
      await github.getByText("Connected", { exact: true }).waitFor();
      await page.getByTestId("connection-notion").getByRole("button", { name: "Disconnect Notion" }).click();
      await page.getByRole("alertdialog", { name: "Disconnect Notion?" }).getByRole("button", { name: "Disconnect" }).click();
      await page.getByTestId("connection-notion").getByText("Not connected").waitFor();
      await page.getByTestId("connection-notion").getByRole("button", { name: "Connect Notion" }).click();
      await page.getByRole("link", { name: "Deny" }).click();
      await page.waitForURL(`${BASE}/account/connections`);
      await page.getByText("Connecting Notion was cancelled. Connect again when you're ready.").waitFor();
      await page.getByTestId("connection-notion").getByText("Not connected").waitFor();
    });

    // ---- keyboard ----------------------------------------------------------------------
    step("Ctrl+Shift+O starts a new chat from anywhere");
    await page.goto(`${BASE}/account`);
    await page.getByRole("heading", { name: "Account", level: 1 }).waitFor();
    await page.keyboard.press("Control+Shift+O");
    await page.waitForURL(`${BASE}/`);
    await page.getByRole("heading", { name: /^What can .+ help with\?$/ }).waitFor();

    step("Keyboard only: Tab to the composer, Shift+Enter for a new line, Enter to send");
    await page.goto(`${BASE}/`);
    await composer().waitFor();
    await page.locator("body").click({ position: { x: 700, y: 5 } });
    await page.keyboard.press("Shift+Tab");
    let reached = false;
    for (let i = 0; i < 40 && !reached; i++) {
      await page.keyboard.press("Tab");
      reached = await page.evaluate(() => document.activeElement?.getAttribute("aria-label")?.startsWith("Message ") ?? false);
    }
    assert.ok(reached, "the composer is reachable with Tab");
    await page.keyboard.type("line one");
    await page.keyboard.press("Shift+Enter");
    await page.keyboard.type("line two");
    assert.equal(await composer().inputValue(), "line one\nline two");
    await page.keyboard.press("Enter");
    await page.getByTestId("user-text").getByText(/line one\s+line two/).waitFor();
    await page.waitForURL(/\/c\//);
    await waitIdle();

    // ---- dark mode -------------------------------------------------------------------
    step("Dark mode from the account menu, remembered, and following the system when asked");
    // A chat with a tool call on the fixture; the one this run started elsewhere.
    const someChat = fixture ? "c_adapricing01" : firstId;
    await page.goto(`${BASE}/c/${encodeURIComponent(someChat)}`);
    await settled();
    await page.getByRole("button", { name: /^Account menu/ }).click();
    await page.getByRole("menuitemradio", { name: "Dark" }).click();
    await page.keyboard.press("Escape");
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark");
    await page.reload();
    await composer().waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark");
    await shot(page, "app-17-dark-conversation");
    await axe(page, "conversation (dark)");
    await page.goto(`${BASE}/`);
    await settled();
    await shot(page, "app-18-dark-new-chat");
    await axe(page, "new chat (dark)");
    await onlyFixture("dark markdown", async () => {
      await send("Show me a table and code");
      await waitIdle();
      await lastAssistant().locator(".shiki").first().waitFor();
      await page.waitForTimeout(200);
      await shot(page, "app-18b-dark-markdown");
      await axe(page, "markdown and code (dark)");
      await page.goto(`${BASE}/`);
      await settled();
    });
    await page.getByRole("button", { name: /^Account menu/ }).click();
    await page.getByRole("menuitemradio", { name: "Match system" }).click();
    await page.keyboard.press("Escape");
    await page.emulateMedia({ colorScheme: "dark" });
    await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
    await page.emulateMedia({ colorScheme: "light" });
    await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
    await page.goto(`${BASE}/c/${encodeURIComponent(someChat)}`);
    await composer().waitFor();
    await axe(page, "conversation (light)");

    // ---- phone -------------------------------------------------------------------------
    step("On a 360 px phone nothing scrolls sideways and the sidebar is a drawer that Escape closes");
    const phone = await browser.newContext({ viewport: { width: 360, height: 740 }, isMobile: true, hasTouch: true });
    await phone.addCookies(await context.cookies());
    const mobile = await phone.newPage();
    watch(mobile);
    await mobile.goto(`${BASE}/`);
    await mobile.getByRole("heading", { name: /^What can .+ help with\?$/ }).waitFor();
    await noHorizontalScroll(mobile, "phone new chat");
    if (fixture) {
      // The empty state starts at the top, with the agent cards in view.
      await mobile.waitForTimeout(300);
      assert.equal(await mobile.getByTestId("thread-viewport").evaluate((el) => el.scrollTop), 0);
      await mobile.getByRole("radiogroup", { name: "Choose an agent" }).waitFor();
    }
    await shot(mobile, "app-19-phone-new-chat");
    await mobile.getByRole("button", { name: "Open navigation" }).click();
    const drawer = mobile.getByRole("dialog", { name: "Conversations" });
    await drawer.waitFor();
    await shot(mobile, "app-20-phone-drawer");
    await mobile.keyboard.press("Escape");
    await drawer.waitFor({ state: "detached" });
    if (fixture) {
      await mobile.goto(`${BASE}/c/c_adapricing01`);
      await mobile.getByText("covers up to 25 people", { exact: false }).waitFor();
      await noHorizontalScroll(mobile, "phone conversation");
      await mobile.getByRole("textbox", { name: /^Message / }).fill("Show me a table and code");
      await mobile.getByRole("textbox", { name: /^Message / }).press("Enter");
      await mobile.getByTestId("send").waitFor();
      await noHorizontalScroll(mobile, "phone conversation with a table");
      await shot(mobile, "app-21-phone-conversation");
    }
    await mobile.goto(`${BASE}/account`);
    await mobile.getByRole("heading", { name: "Account", level: 1 }).waitFor();
    await noHorizontalScroll(mobile, "phone account");
    await mobile.goto(`${BASE}/account/connections`);
    await mobile.getByRole("heading", { name: "Connections", level: 1 }).waitFor();
    await mobile.getByText(/Connect your own accounts/).waitFor();
    await noHorizontalScroll(mobile, "phone connections");
    await shot(mobile, "app-22-phone-connections");
    await phone.close();

    // ---- error states --------------------------------------------------------------------
    await onlyFixture("error states", async () => {
      step("Missing chats, other people's chats, and unreachable servers have plain states");
      await page.goto(`${BASE}/c/c_doesnotexist01`);
      await page.getByRole("heading", { name: "This chat doesn't exist or was deleted." }).waitFor();
      await page.goto(`${BASE}/c/c_sampricing01`);
      await page.getByRole("heading", { name: "You don't have access to this chat." }).waitFor();
      await shot(page, "app-23-forbidden");
      await page.goto(`${BASE}/`);
      await context.route("**/api/chat", (route) => route.abort("failed"));
      await send("Is anyone there?");
      await lastAssistant().getByText("Can't reach Hubzoid. Check your connection and try again.", { exact: false }).waitFor();
      await context.unroute("**/api/chat");
      await fetch(`${BASE}/__fixture/flag/chat_fail/true`);
      await send("Try once more");
      await lastAssistant().getByText("The agent is unavailable right now. Try again in a minute.", { exact: false }).waitFor();
      await fetch(`${BASE}/__fixture/flag/chat_fail/false`);

      step("After replies that never reached the server, the next message still goes through");
      await send("Back online?");
      await lastAssistant().getByText("Back online?", { exact: false }).waitFor();
      await waitIdle();
      const recovered = decodeURIComponent(page.url().split("/c/")[1]);
      const stored = [...fixture.state.messages.values()].filter((m) => m.conversation_id === recovered);
      assert.equal(stored.length, 2, "only the exchange the server saw is stored");
      assert.equal(stored.find((m) => m.role === "user").parent_id, null);

      step("One reply at a time: a second tab is asked to wait, in plain words");
      const otherTab = await context.newPage();
      watch(otherTab);
      await otherTab.goto(`${BASE}/c/c_adapricing01`);
      await otherTab.getByRole("textbox", { name: /^Message / }).waitFor();
      await page.goto(`${BASE}/c/c_adapricing01`);
      await settled();
      await send("give me a slow answer");
      await lastAssistant().getByText("Step 2 of the long answer.", { exact: false }).waitFor();
      await otherTab.getByRole("textbox", { name: /^Message / }).fill("Are you there?");
      await otherTab.getByRole("textbox", { name: /^Message / }).press("Enter");
      await otherTab
        .locator('[data-role="assistant"]')
        .last()
        .getByText("Another reply is still being written in this chat. Wait for it to finish, then send again.", { exact: false })
        .waitFor();
      assert.ok(requests().filter((r) => r.path === "/api/chat").length >= 2);
      await page.getByRole("button", { name: "Stop response" }).click();
      await lastAssistant().getByTestId("stopped").waitFor();
      await otherTab.close();

      step("A page that fails to load (say, after an update) offers Reload instead of a blank screen");
      const before = errors.length;
      await page.goto(`${BASE}/`);
      await settled();
      await context.route(/\/assets\/ConnectionsPage-[^/]+\.js$/, (route) => route.abort("failed"));
      await page.getByRole("button", { name: /^Account menu/ }).click();
      await page.getByRole("menuitem", { name: "Connections" }).click();
      await page.getByRole("heading", { name: "This page stopped working" }).waitFor();
      await page.getByRole("navigation", { name: "Conversations" }).waitFor();
      await shot(page, "app-23b-page-error");
      await context.unroute(/\/assets\/ConnectionsPage-[^/]+\.js$/);
      await page.getByRole("button", { name: "Reload" }).click();
      await page.getByRole("heading", { name: "Connections", level: 1 }).waitFor();
      errors.splice(before); // the failed chunk is logged on purpose

      step("Someone with no agents is told how to get access");
      await fetch(`${BASE}/__fixture/flag/no_agents/true`);
      await page.goto(`${BASE}/`);
      await page.getByText("You don't have access to an agent yet. Ask an administrator to give you access, then reload this page.").waitFor();
      await shot(page, "app-24-no-agents");
      await fetch(`${BASE}/__fixture/flag/no_agents/false`);

      step("A one-time link sets a password and signs in; a used link says so");
      const fresh = await browser.newContext({ viewport: { width: 1200, height: 860 } });
      const invited = await fresh.newPage();
      watch(invited);
      await invited.goto(`${BASE}/auth/set-password?token=set-token-valid-0001`);
      await invited.getByRole("heading", { name: "Set your password" }).waitFor();
      await invited.getByText("For sam@example.com").waitFor();
      await axe(invited, "set password");
      await invited.getByLabel("New password").fill("short");
      await invited.getByRole("button", { name: "Save password and sign in" }).click();
      await invited.getByText("Use at least 8 characters for your password.").waitFor();
      await invited.getByLabel("New password").fill("a-long-password");
      await invited.getByLabel("Confirm password").fill("a-long-password");
      await invited.getByRole("button", { name: "Save password and sign in" }).click();
      await invited.waitForURL(`${BASE}/`);
      await invited.getByRole("heading", { name: /^What can .+ help with\?$/ }).waitFor();
      await invited.goto(`${BASE}/auth/set-password?token=set-token-valid-0001`);
      await invited.getByRole("heading", { name: "This link can't be used" }).waitFor();

      step("Signing out ends the session; self sign-up creates an account or waits for approval");
      await invited.goto(`${BASE}/`);
      await invited.getByRole("button", { name: /^Account menu/ }).click();
      await invited.getByRole("menuitem", { name: "Sign out" }).click();
      await invited.waitForURL(`${BASE}/auth`);
      await invited.getByRole("button", { name: "Create an account" }).click();
      await invited.getByLabel("Your name").fill("Lee Park");
      await invited.getByLabel("Email").fill("lee@other.org");
      await invited.getByLabel("Password").fill("lee-password");
      await invited.getByRole("button", { name: "Create account" }).click();
      await invited.getByText(/Your account is waiting for an administrator to approve it/).waitFor();
      await invited.getByRole("button", { name: "Create an account" }).click();
      await invited.getByLabel("Your name").fill("Noor Haddad");
      await invited.getByLabel("Email").fill("noor@example.com");
      await invited.getByLabel("Password").fill("noor-password");
      await invited.getByRole("button", { name: "Create account" }).click();
      await invited.waitForURL(`${BASE}/`);
      await invited.getByRole("button", { name: /^Account menu: Noor Haddad/ }).waitFor();
      await fresh.close();

      step("A session that ends mid-use goes back to sign-in, then returns to the same chat");
      await fetch(`${BASE}/__fixture/expire`);
      await page.goto(`${BASE}/c/c_adastandup01`);
      await page.waitForURL(/\/auth\?redirect=%2Fc%2Fc_adastandup01/);
      await signIn(page);
      await page.waitForURL(`${BASE}/c/c_adastandup01`);
      await page.getByTestId("chat-title").getByText("Standup agent plan").waitFor();

      step("Branding from the hub: name, logo, favicon and stylesheet");
      await fetch(`${BASE}/__fixture/flag/branded/true`);
      await page.goto(`${BASE}/`);
      await settled();
      const brandLink = page.getByRole("navigation", { name: "Conversations" }).getByRole("link", { name: "Acme Support" });
      await brandLink.waitFor();
      assert.equal(await brandLink.locator("img").getAttribute("src"), "/branding/logo.svg");
      await page.waitForFunction(() => document.title.endsWith("· Acme Support"));
      assert.equal(await page.evaluate(() => document.querySelector('link[rel="icon"]').getAttribute("href")), "/branding/favicon.svg");
      assert.equal(await page.evaluate(() => document.querySelector("link[data-hz-branding]").getAttribute("href")), "/branding/custom.css");
      assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--acme-brand").trim()), "#1f6f5c");
      await shot(page, "app-26-branding");
      await fetch(`${BASE}/__fixture/flag/branded/false`);

      step("External sign-in returns to the page that asked; cancelling it says so");
      const viaGoogle = await browser.newContext({ viewport: { width: 1200, height: 860 } });
      const gp = await viaGoogle.newPage();
      watch(gp);
      await gp.goto(`${BASE}/account`);
      await gp.waitForURL(/\/auth\?redirect=%2Faccount/);
      await gp.getByRole("link", { name: "Continue with Google" }).click();
      await gp.getByRole("link", { name: "Cancel" }).click();
      await gp.getByText("Sign-in was cancelled. Try again when you're ready.").waitFor();
      await gp.goto(`${BASE}/auth?redirect=%2Faccount`);
      await gp.getByRole("link", { name: "Continue with Google" }).click();
      await gp.getByRole("link", { name: "sam@example.com" }).click();
      await gp.waitForURL(`${BASE}/account`);
      await gp.getByText("sam@example.com").first().waitFor();
      await viaGoogle.close();

    });

    // ---- local mode ----------------------------------------------------------------------
    if (local) {
      step("Local mode: no sign-in page, no Sign out, and the account page explains why");
      const lctx = await browser.newContext({ viewport: { width: 1360, height: 900 } });
      const lp = await lctx.newPage();
      watch(lp);
      await lp.goto(`${LOCAL_BASE}/`);
      await lp.getByRole("heading", { name: /^What can .+ help with\?$/ }).waitFor();
      assert.equal(new URL(lp.url()).pathname, "/");
      await lp.getByRole("button", { name: /^Account menu: Local mode/ }).click();
      await lp.getByRole("menuitem", { name: "Admin Console" }).waitFor();
      assert.equal(await lp.getByRole("menuitem", { name: "Sign out" }).count(), 0);
      await lp.keyboard.press("Escape");
      await lp.goto(`${LOCAL_BASE}/account`);
      await lp.getByText(/Sign-in is off on this server/).waitFor();
      assert.equal(await lp.getByRole("button", { name: "Sign out" }).count(), 0);
      await lp.goto(`${LOCAL_BASE}/auth`);
      await lp.waitForURL(`${LOCAL_BASE}/`);
      await lp.getByRole("heading", { name: /^What can .+ help with\?$/ }).waitFor();
      await shot(lp, "app-25-local-mode");
      step("The Console still loads from /portal/ in the same bundle");
      await lp.goto(`${LOCAL_BASE}/portal/`);
      await lp.getByRole("heading", { name: "Agents", level: 1 }).waitFor();
      assert.equal(await lp.title(), "Hubzoid Admin Console");
      await lctx.close();
    }

    assert.deepEqual(errors, [], "no console or page errors");
    console.log(`\nPASS: ${steps.length} journeys${skipped.length ? `, ${skipped.length} skipped` : ""}. Screenshots: ${shots}`);
  } catch (error) {
    if (diagnosticPage) {
      fs.writeFileSync(path.join(shots, "app-journey-failure.html"), await diagnosticPage.content().catch(() => ""));
      await diagnosticPage.screenshot({ path: path.join(shots, "app-journey-failure.png") }).catch(() => {});
    }
    if (errors.length) console.error("console errors:", errors);
    throw error;
  } finally {
    await browser.close();
    await fixture?.close();
    await local?.close();
  }
})().catch((error) => {
  console.error(`\nFAIL at: ${steps[steps.length - 1]}\n`, error);
  process.exit(1);
});
