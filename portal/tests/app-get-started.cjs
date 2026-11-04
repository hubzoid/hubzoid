// Get started cards on the empty chat (chat/GetStarted.tsx): the cards and
// their badges, the fix help, Check again, Hide and the link that brings them
// back, and nothing at all on 403 or 404. The endpoint is mocked with the
// contract's JSON; the rest is the in-memory fixture with sign-in off.
//   node tests/app-get-started.cjs
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { createApp, AGENTS } = require("./app-fixture-server.cjs");

const ANSWER = {
  title: "Get started",
  ready: false,
  checked_at: "2026-11-04T12:00:00Z",
  cards: [
    { id: "model", title: "Model", status: "ok", line: "Claude Code, Team plan", detail: "Codex also signed in (Plus plan)", fix: null },
    { id: "requests", title: "Requests", status: "info", line: "Chat and MCP", detail: null, fix: null },
    { id: "builds", title: "Builds", status: "warn", line: "Docker not running", detail: "Plans and builds run in Docker",
      fix: "Start Docker, then run hubzoid sandbox doctor ." },
    { id: "code", title: "Code", status: "fail", line: "acme/api", detail: "Not connected",
      fix: "Connect GitHub with `hubzoid connections add github`." },
    { id: "results", title: "Results", status: null, line: "Reviewed branches", detail: "After you approve the plan", fix: null },
  ],
};

(async () => {
  const agent = { ...AGENTS[0], get_started: true };
  const fixture = createApp({ mode: "local", agents: [agent, { ...AGENTS[1] }] });
  const base = `http://127.0.0.1:${await fixture.listen(0)}`;
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: "block" });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    let reply = { status: 200, body: ANSWER };
    let hold = null;
    const asked = [];
    await page.route(`**/api/hubs/${agent.hub}/get-started*`, async (route) => {
      asked.push(route.request().url());
      if (hold) await hold;
      await route.fulfill({ status: reply.status, contentType: "application/json", body: JSON.stringify(reply.body) });
    });

    await page.goto(`${base}/`);
    const cards = page.getByRole("region", { name: "Get started" });
    await cards.getByText("2 need you").waitFor();
    assert.equal(await cards.getByRole("listitem").count(), 5);
    for (const [title, badge] of [["Model", "Ready"], ["Requests", "Ready"], ["Builds", "Check"], ["Code", "Needs you"]])
      await cards.getByRole("listitem").filter({ hasText: title }).getByText(badge, { exact: true }).waitFor();
    assert.equal(await cards.locator('[data-status="static"] .hz-pill').count(), 0, "a static card has no badge");
    assert.ok(await page.locator("textarea.hz-composer-input").isVisible(), "the composer is there");
    assert.ok(await page.getByRole("button", { name: AGENTS[0].suggestions[0] }).isVisible(), "the suggestions stay");

    // The fix: commands in monospace, quoted or not.
    await cards.getByRole("button", { name: "How to fix: Builds" }).click();
    assert.equal(await page.locator(".hz-gs-fix code").textContent(), "hubzoid sandbox doctor .");
    await page.keyboard.press("Escape");
    await page.locator(".hz-gs-fix").waitFor({ state: "detached" });
    await cards.getByRole("button", { name: "How to fix: Code" }).click();
    assert.equal(await page.locator(".hz-gs-fix code").textContent(), "hubzoid connections add github");
    await page.keyboard.press("Escape");
    await page.locator(".hz-gs-fix").waitFor({ state: "detached" });

    // Check again skips the server's cache and shows Checking… meanwhile.
    let release;
    hold = new Promise((r) => (release = r));
    reply = { status: 200, body: { ...ANSWER, ready: true, cards: ANSWER.cards.map((c) => (c.status ? { ...c, status: "ok", fix: null } : c)) } };
    await cards.getByRole("button", { name: "Check again" }).click();
    await cards.getByText("Checking…").first().waitFor();
    assert.match(asked.at(-1), /\?refresh=1$/);
    release();
    hold = null;
    await cards.getByText("All set").waitFor();

    // Hide is remembered for this agent; the link brings the cards back.
    await cards.getByRole("button", { name: "Hide" }).click();
    await page.getByRole("button", { name: "Get started", exact: true }).waitFor();
    assert.equal(await cards.count(), 0);
    const before = asked.length;
    await page.reload();
    await page.getByRole("button", { name: "Get started", exact: true }).waitFor();
    assert.equal(asked.length, before, "hidden cards are not checked");
    await page.getByRole("button", { name: "Get started", exact: true }).click();
    await cards.getByText("All set").waitFor();

    // An invalid file is one notice; 403 and 404 show nothing.
    reply = { status: 200, body: { error: "Card code: no doctor check factory.gh.", cards: [] } };
    await page.reload();
    await page.getByText("Get started cards need fixing.").waitFor();
    for (const status of [403, 404]) {
      reply = { status, body: { detail: { code: status === 403 ? "forbidden" : "not_found", message: "No." } } };
      const n = asked.length;
      await page.reload();
      await page.locator("textarea.hz-composer-input").waitFor();
      for (let i = 0; asked.length === n && i < 100; i++) await page.waitForTimeout(50);
      assert.ok(asked.length > n, "the cards were asked for");
      await page.waitForTimeout(600);
      assert.equal(await page.locator(".hz-gs, .hz-gs-show, .hz-gs-notice").count(), 0, `nothing on ${status}`);
    }
    assert.deepEqual(errors, []);
    console.log("get started cards: ok");
  } finally {
    await browser.close();
    await fixture.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
