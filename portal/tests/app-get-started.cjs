// Get started cards on the empty chat (chat/GetStarted.tsx): nothing before
// the first answer, the cards and their badges, the fix help (only backticks
// are monospace), Check again and Hide keeping focus, the link that brings
// the cards back, the invalid-file notice, and nothing at all on 403 or 404,
// remembered for the page. The endpoint is mocked with the contract's JSON;
// the rest is the in-memory fixture with sign-in off.
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
      fix: "Start Docker, then run hubzoid channels doctor . github" },
    { id: "code", title: "Code", status: "fail", line: "acme/api", detail: "Not connected",
      fix: "Connect GitHub with `hubzoid connections add github`." },
    // No status field at all: a static card, like status null.
    { id: "results", title: "Results", line: "Reviewed branches", detail: "After you approve the plan", fix: null },
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
    let release = () => {};
    const holdNext = () => (hold = new Promise((r) => (release = () => { hold = null; r(); })));
    const asked = [];
    await page.route(`**/api/hubs/${agent.hub}/get-started*`, async (route) => {
      asked.push(route.request().url());
      if (hold) await hold;
      await route.fulfill({ status: reply.status, contentType: "application/json", body: JSON.stringify(reply.body) });
    });
    const focused = (locator) => locator.evaluate((el) => el === document.activeElement);

    // Nothing at all until the first answer: no heading, no Checking….
    holdNext();
    await page.goto(`${base}/`);
    await page.locator("textarea.hz-composer-input").waitFor();
    for (let i = 0; asked.length === 0 && i < 100; i++) await page.waitForTimeout(50);
    await page.waitForTimeout(600);
    assert.equal(await page.locator(".hz-gs, .hz-gs-show, .hz-gs-notice").count(), 0, "nothing before the first answer");
    const composerAt = (await page.locator("textarea.hz-composer-input").boundingBox()).y;
    release();

    const cards = page.getByRole("region", { name: "Get started" });
    await cards.getByText("2 need you").waitFor();
    assert.equal((await page.locator("textarea.hz-composer-input").boundingBox()).y, composerAt, "the composer stays put");
    assert.equal(await cards.getByRole("listitem").count(), 5);
    for (const [title, badge] of [["Model", "Ready"], ["Requests", "Ready"], ["Builds", "Check"], ["Code", "Needs you"]])
      await cards.getByRole("listitem").filter({ hasText: title }).getByText(badge, { exact: true }).waitFor();
    assert.equal(await cards.locator('[data-status="static"] .hz-pill').count(), 0, "a static card has no badge");
    assert.ok(await page.locator("textarea.hz-composer-input").isVisible(), "the composer is there");
    assert.ok(await page.getByRole("button", { name: AGENTS[0].suggestions[0] }).isVisible(), "the suggestions stay");

    // The fix: only what is in backticks is monospace.
    await cards.getByRole("button", { name: "How to fix: Builds" }).click();
    await page.locator(".hz-gs-fix").getByText("hubzoid channels doctor . github").waitFor();
    assert.equal(await page.locator(".hz-gs-fix code").count(), 0, "a plain command stays plain");
    await page.keyboard.press("Escape");
    await page.locator(".hz-gs-fix").waitFor({ state: "detached" });
    await cards.getByRole("button", { name: "How to fix: Code" }).click();
    assert.equal(await page.locator(".hz-gs-fix code").textContent(), "hubzoid connections add github");
    await page.keyboard.press("Escape");
    await page.locator(".hz-gs-fix").waitFor({ state: "detached" });

    // Check again skips the server's cache, shows Checking… meanwhile, keeps
    // focus and ignores a second press.
    holdNext();
    reply = { status: 200, body: { ...ANSWER, ready: true, cards: ANSWER.cards.map((c) => (c.status ? { ...c, status: "ok", fix: null } : c)) } };
    const again = cards.getByRole("button", { name: "Check again" });
    await again.focus();
    await page.keyboard.press("Enter");
    await cards.getByText("Checking…").first().waitFor();
    assert.match(asked.at(-1), /\?refresh=1$/);
    assert.equal(await again.getAttribute("aria-disabled"), "true");
    assert.ok(await focused(again), "Check again keeps focus while it asks");
    const n = asked.length;
    await page.keyboard.press("Enter");
    await page.waitForTimeout(100);
    assert.equal(asked.length, n, "a second press while asking is ignored");
    release();
    await cards.getByText("All set").waitFor();
    assert.ok(await focused(again), "Check again keeps focus after the answer");

    // Hide is remembered for this agent; focus goes to the link that brings
    // the cards back, and from it to Hide.
    const showLink = page.getByRole("button", { name: "Get started", exact: true });
    const hide = cards.getByRole("button", { name: "Hide Get started" });
    await hide.focus();
    await page.keyboard.press("Enter");
    await showLink.waitFor();
    assert.ok(await focused(showLink), "Hide moves focus to Get started");
    assert.equal(await cards.count(), 0);
    await page.keyboard.press("Enter");
    await cards.getByText("All set").waitFor();
    assert.ok(await focused(hide), "Get started moves focus to Hide");
    await hide.click();
    const before = asked.length;
    await page.reload();
    await showLink.waitFor();
    assert.equal(asked.length, before, "hidden cards are not checked");
    holdNext();
    await showLink.click();
    await page.waitForTimeout(200);
    assert.ok(await focused(showLink), "the link stays, focused, until the cards come");
    release();
    await cards.getByText("All set").waitFor();
    assert.ok(await focused(hide), "then Hide has focus");

    // An invalid file is one notice, with Check again and Hide; an answer
    // that brings the cards keeps focus on Check again.
    reply = { status: 200, body: { error: "Card code: no doctor check factory.gh.", cards: [] } };
    await page.reload();
    const notice = page.locator(".hz-gs-notice");
    await notice.getByText("Get started cards need fixing.").waitFor();
    await notice.getByRole("button", { name: "Why the cards need fixing" }).waitFor();
    await notice.getByRole("button", { name: "Hide Get started" }).waitFor();
    reply = { status: 200, body: ANSWER };
    await notice.getByRole("button", { name: "Check again" }).focus();
    await page.keyboard.press("Enter");
    await cards.getByText("2 need you").waitFor();
    assert.ok(await focused(again), "the cards' Check again takes focus from the notice's");

    // 403 and 404 show nothing, and a new chat on the page doesn't ask again.
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
    const n404 = asked.length;
    await page.getByRole("button", { name: "New chat" }).first().click();
    await page.locator("textarea.hz-composer-input").waitFor();
    await page.waitForTimeout(600);
    assert.equal(asked.length, n404, "a 404 is remembered for the page");
    assert.equal(await page.locator(".hz-empty-top").count(), 0, "and the next empty chat is centred");
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
