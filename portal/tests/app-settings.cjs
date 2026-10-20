// Focus/navigation regressions after lazy-loading the embedded Console CSS.
// Uses synthetic accounts and the existing local OAuth fixture, never Google.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { createApp, PASSWORD } = require("./app-fixture-server.cjs");

(async () => {
  const fixture = createApp({ mode: "accounts" });
  const dev = process.env.APP_DEV ? await require("./app-dev-server.cjs").attachDev(fixture) : null;
  const base = `http://127.0.0.1:${await fixture.listen(0)}`;
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ viewport: { width: 1470, height: 860 }, serviceWorkers: "block" });
    const page = await context.newPage();
    await page.goto(`${base}/auth`);
    await page.getByLabel("Email", { exact: true }).fill("ada@example.com");
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.waitForURL(`${base}/`);
    await page.goto(`${base}/settings#/me/profile`);
    await page.getByLabel("Display name").waitFor();
    const sections = page.locator(".hz-set-nav");
    assert.equal(await sections.getByRole("link", { name: "Preferences", exact: true }).count(), 1);
    assert.equal(await sections.getByRole("link", { name: "Appearance", exact: true }).count(), 0);
    await page.getByRole("navigation", { name: "Personal preferences" }).getByRole("link", { name: "Appearance", exact: true }).click();
    await page.getByRole("radio", { name: "Dark", exact: true }).click();
    await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
    await page.getByLabel("Search settings").fill("name");
    await sections.getByRole("link", { name: "Profile", exact: true }).click();
    await page.getByLabel("Display name").fill("Ada Review");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await page.getByText("Name saved.").waitFor();
    assert.equal(fixture.state.users.find((u) => u.email === "ada@example.com").name, "Ada Review");
    await page.getByLabel("Search settings").fill("no-such-preference");
    await page.getByText(/No settings match/).waitFor();
    await page.getByLabel("Search settings").fill("");
    await sections.getByRole("link", { name: "Workspace settings", exact: true }).click();
    await page.locator(".console-embed").waitFor({timeout: 60000});
    await page.getByRole("navigation", { name: "Workspace settings", exact: true }).getByRole("link", { name: "Brand", exact: true }).click();
    await page.waitForURL(/#\/settings\/appearance$/);
    await page.getByRole("navigation", { name: "Workspace settings", exact: true }).getByRole("link", { name: "Health & support", exact: true }).waitFor();
    assert.equal(await sections.getByRole("link", { name: "Health & support", exact: true }).count(), 0);
    await sections.getByRole("link", { name: "Agents", exact: true }).click();
    await page.getByRole("list", { name: "Agents", exact: true }).waitFor();
    assert.equal(await page.getByRole("list", { name: "Totals", exact: true }).isVisible(), false);
    await page.getByText("Usage overview", { exact: true }).click();
    await page.getByRole("list", { name: "Totals", exact: true }).waitFor();
    await page.setViewportSize({ width: 1172, height: 740 });
    await sections.getByRole("link", { name: "People & access", exact: true }).click();
    await page.locator(".person-cell").first().waitFor();
    const identities = await page.locator(".person-cell").evaluateAll((cells) => cells.map((cell) => ({
      width: cell.getBoundingClientRect().width,
      left: cell.getBoundingClientRect().left,
      avatarRight: cell.parentElement.previousElementSibling.getBoundingClientRect().right,
    })));
    assert.ok(identities.every((cell) => cell.width >= 170 && cell.left >= cell.avatarRight + 5), "person names and avatars do not overlap in embedded Settings");
    await page.goto(`${base}/settings#/agents/finance/access`);
    await page.getByRole("list", { name: "Access to Finance Assistant", exact: true }).waitFor();
    const agentNav = page.getByRole("navigation", { name: "Agent sections" });
    const runsLink = agentNav.getByRole("link", { name: "Runs & schedules", exact: true });
    await runsLink.focus();
    await page.keyboard.press("Enter");
    await page.getByRole("list", { name: "Workflows", exact: true }).waitFor();
    assert.equal(await runsLink.getAttribute("aria-current"), "page");
    await page.setViewportSize({ width: 390, height: 844 });
    const workflowRows = page.locator(".hz-workflow-list > li");
    const actionBounds = await workflowRows.first().getByRole("button", { name: "Run now", exact: true }).boundingBox();
    assert.ok(actionBounds && actionBounds.x >= 0 && actionBounds.x + actionBounds.width <= 390, "workflow actions fit without horizontal scrolling");
    await agentNav.getByRole("link", { name: "Access", exact: true }).click();
    const editAccess = page.getByRole("button", { name: /^Edit access for/ }).first();
    await editAccess.waitFor();
    const editBounds = await editAccess.boundingBox();
    assert.ok(editBounds && editBounds.x + editBounds.width <= 390 && editBounds.height >= 44, "access actions are reachable on phones");
    await page.setViewportSize({ width: 1172, height: 740 });
    // Navigate within the app: the lazy Console stylesheet must remain loaded.
    await page.locator(".hz-set-backlink").click();
    const input = page.getByRole("textbox", { name: /^Message / });
    await input.waitFor();
    await input.focus();
    assert.equal(await input.evaluate((el) => getComputedStyle(el).outlineStyle), "none");
    assert.equal(await page.locator(".hz-composer").evaluate((el) => getComputedStyle(el).outlineStyle), "solid");
    await page.keyboard.press("Control+k");
    const search = page.locator(".hz-search-input input");
    await search.waitFor();
    await search.focus();
    assert.equal(await search.evaluate((el) => getComputedStyle(el).outlineStyle), "none");
    assert.equal(await page.locator(".hz-search-input").evaluate((el) => getComputedStyle(el).outlineStyle), "solid");
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${base}/settings#/me/profile`);
    await page.getByLabel("Display name").waitFor();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.ok((await page.getByLabel("Display name").boundingBox()).height >= 44);
    await page.locator(".hz-set-back").click();
    await sections.getByRole("link", { name: "Preferences", exact: true }).waitFor();

    const guest = await browser.newContext({ serviceWorkers: "block" });
    const oauth = await guest.newPage();
    await oauth.goto(`${base}/auth?redirect=%2Faccount`);
    await oauth.getByRole("link", { name: "Continue with Google" }).click();
    await oauth.getByRole("link", { name: "Cancel", exact: true }).click();
    await oauth.getByText("Sign-in was cancelled. Try again when you're ready.").waitFor();
    await oauth.goto(`${base}/auth?redirect=%2Faccount`);
    await oauth.getByRole("link", { name: "Continue with Google" }).click();
    await oauth.getByRole("link", { name: "sam@example.com", exact: true }).click();
    await oauth.getByLabel("Display name").waitFor();
    await oauth.getByText("sam@example.com", { exact: true }).waitFor();
    console.log("Settings grouping, profile save, focus after Console, phone layout and fixture OAuth passed.");
  } finally {
    await browser.close();
    await dev?.close();
    await fixture.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
