// Browser regression checks for the agent-first portal. They run the built
// assets (hubzoid/portal_dist) against the synthetic fixture in fixture.cjs
// via request interception: no server, no customer data, no live mutations.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createFixture, perm } = require("./fixture.cjs");

const root = path.resolve(__dirname, "../../hubzoid/portal_dist");
const shots = process.env.PORTAL_SHOTS || path.join(require("node:os").tmpdir(), "hubzoid-console-tests");
fs.mkdirSync(shots, { recursive: true });
const ORIGIN = "http://hubzoid.test";
const PRIYA = "priya.natarajan@example.org";
const USE_HUB_PERM = "use_hub";
const EVERYONE_SUBJECT = "*";

const steps = [];
function step(name) {
  steps.push(name);
  console.log(`· ${name}`);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const fixture = createFixture();
  const { state } = fixture;
  let diagnosticPage;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
    const page = await context.newPage();
    diagnosticPage = page;
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("console", (m) => {
      // The browser logs "Failed to load resource" for every non-2xx fetch. The
      // negative-path journeys intentionally provoke 4xx/5xx (blocked grant,
      // mid-save failure, hub-admin forbidden) and the app handles them in-UI.
      // Keep only genuine JS/console errors, not expected HTTP-status noise.
      if (m.type() === "error" && !/Failed to load resource/.test(m.text()))
        errors.push(m.text());
    });
    // The fixture-backed router, reusable so a second (clock-controlled) context can
    // serve the same synthetic API. `onApi(url, request)` observes each API call.
    const serve = (onApi) => async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.startsWith("/portal/api")) {
        const endpoint = url.pathname.slice("/portal/api".length);
        if (onApi) onApi(url, request);
        try {
          const data = await fixture.handle(
            request.method(),
            endpoint,
            Object.fromEntries(url.searchParams),
            request.method() === "POST" ? request.postDataJSON() : undefined,
          );
          return route.fulfill({ json: data });
        } catch (e) {
          return route.fulfill({ status: e.status || 500, json: { detail: e.detail || String(e) } });
        }
      }
      if (url.pathname === "/" || url.pathname === "/admin")
        return route.fulfill({ body: "<title>placeholder</title>", contentType: "text/html" });
      const relative = url.pathname.replace(/^\/portal\/?/, "") || "index.html";
      const file = path.join(root, relative);
      const exists = fs.existsSync(file) && fs.statSync(file).isFile();
      return route.fulfill({
        body: fs.readFileSync(exists ? file : path.join(root, "index.html")),
        contentType: file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html",
      });
    };
    await context.route(`${ORIGIN}/**`, serve());
    const go = (hash) => page.goto(`${ORIGIN}/portal/${hash ? "#" + hash : ""}`);
    const hash = () => page.evaluate(() => location.hash);
    const drawer = () => page.locator(".ant-drawer-section[role=dialog]");
    const modal = () => page.locator(".ant-modal-confirm").filter({ visible: true });
    const modalTitle = (text) => modal().locator(".ant-modal-confirm-title").getByText(text);
    // Click a confirm-dialog button and wait for the dialog to finish leaving,
    // so a follow-up dialog is never confused with the fading one.
    const answer = async (name) => {
      await modal().getByRole("button", { name }).click();
      await page.waitForFunction(() => document.querySelectorAll(".ant-modal-confirm").length === 0);
    };
    const lastMutation = () => state.mutations[state.mutations.length - 1];
    // Optional capability groups start collapsed; their header is a real button.
    const group = (name) => drawer().getByRole("button", { name: new RegExp(`^${name}`) });
    const expand = async (name) => {
      const toggle = group(name);
      if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
    };
    const headerText = async (name) => (await group(name).textContent()).replace(/\s+/g, " ").trim();
    // The element a disclosure button controls (aria-controls).
    const controlled = async (button) => page.locator(`[id="${await button.getAttribute("aria-controls")}"]`);
    // A successful save closes the drawer and shows one compact notice.
    const saved = async () => {
      await drawer().waitFor({ state: "hidden" });
      await page.getByText("Access updated", { exact: true }).waitFor();
      assert.equal(await page.getByText(/about 30 seconds/).count(), 0, "no implementation detail on success");
    };

    // ---- landing -----------------------------------------------------------
    step("The landing page combines five totals and agent cards, with compact navigation");
    await go("");
    await page.getByRole("heading", { name: "Agents", level: 1 }).waitFor();
    assert.equal(await page.title(), "Hubzoid Admin Console");
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "1440px landing has no clipped content");
    const totals = page.getByRole("list", { name: "Totals" });
    await totals.getByText("Across 252 conversations").waitFor();
    assert.equal(await totals.getByRole("listitem").count(), 5);
    await totals.getByText("Workflow runs").waitFor();
    assert.equal(await page.getByRole("link", { name: "Overview", exact: true }).count(), 0);
    assert.equal(await page.getByRole("link", { name: "Runs", exact: true }).count(), 0);
    assert.equal(await page.getByRole("link", { name: /Manage accounts/ }).count(), 0);
    assert.equal(await page.getByText(/Counting since|Snapshot|How people are using/).count(), 0);
    const boxes = await totals.getByRole("listitem").evaluateAll(nodes => nodes.map(n => n.getBoundingClientRect().top));
    assert.ok(boxes.every(y => y === boxes[0]), "Five totals fit on one desktop row");
    const costHelp = page.getByRole("button", { name: "About Approx. cost" });
    await costHelp.focus();
    await page.getByRole("tooltip").filter({ hasText: "Subscription-backed" }).waitFor();
    await page.getByRole("heading", { name: "Agents", level: 1 }).click();
    await costHelp.click();
    await page.getByRole("tooltip").filter({ hasText: "Subscription-backed" }).waitFor();
    await page.getByRole("heading", { name: "Agents", level: 1 }).click();
    const financeUsage = page.getByLabel("Finance Assistant usage", { exact: true });
    await financeUsage.getByText("$11.04", { exact: true }).waitFor();
    await financeUsage.getByText("1.2M", { exact: true }).waitFor();
    await page.getByLabel("Support Assistant usage", { exact: true }).getByText("$23.40*", { exact: true }).waitFor();
    await page.getByLabel("IT Ops Assistant usage", { exact: true }).getByText("—", { exact: true }).waitFor();
    await page.getByText("30 days", { exact: true }).click();
    await totals.getByText("Across 924 conversations").waitFor();
    assert.ok((await hash()).includes("period=30d"), await hash());
    await financeUsage.getByText("$40.48", { exact: true }).waitFor();
    await financeUsage.getByText("4.5M", { exact: true }).waitFor();
    state.failNextGet = { endpoint: "/summary", status: 503, detail: "Temporary outage" };
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await page.getByText("Couldn’t refresh totals", { exact: true }).waitFor();
    await totals.getByText("Across 924 conversations").waitFor();
    await page.getByRole("link", { name: "Finance Assistant", exact: true }).waitFor();
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await page.getByText("Couldn’t refresh totals", { exact: true }).waitFor({ state: "hidden" });
    await page.locator(".updated-at").waitFor();
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-overview.png"), fullPage: true });
    await page.getByRole("link", { name: "Support Assistant", exact: true }).click();
    assert.equal(await hash(), "#/agents/support/access");

    step("Agents lists every agent with its access state and attention items");
    await go("/agents");
    await page.getByRole("heading", { name: "Agents", level: 1 }).waitFor();
    for (const name of ["Finance Assistant", "Support Assistant", "IT Ops Assistant"])
      await page.getByRole("link", { name, exact: true }).waitFor();
    // Simplified landing: attention shows only as per-card tags, no alerts/counts.
    await page.getByText("Legacy access").waitFor();
    await page.getByText("Scheduler stopped").waitFor();
    await page.getByRole("textbox", { name: "Search agents" }).fill("supp");
    assert.equal(await page.getByRole("link", { name: "Finance Assistant", exact: true }).count(), 0);
    await page.getByRole("link", { name: "Support Assistant", exact: true }).waitFor();
    await page.getByRole("textbox", { name: "Search agents" }).fill("");
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-agents.png"), fullPage: true });

    // ---- access: readable rows -----------------------------------------------
    step("Access tab shows readable capabilities, inherited rights and account states");
    await go("/agents/finance/access");
    await page.getByRole("heading", { name: "Access to Finance Assistant" }).waitFor();
    const priyaRow = page.getByRole("row").filter({ hasText: "Priya Natarajan" });
    await priyaRow.getByText("Read ledger", { exact: true }).waitFor();
    await priyaRow.getByText("Manage invoices", { exact: true }).waitFor();
    await priyaRow.getByText("Active", { exact: true }).waitFor();
    await page.getByRole("row").filter({ hasText: "Aisha Rahman" }).getByText("Manage access · inherited").waitFor();
    await page.getByRole("row").filter({ hasText: "daniel.okafor" }).getByText("Not signed up yet").waitFor();
    await page.getByRole("row").filter({ hasText: "monthly_close" }).getByText("Legacy service identity", { exact: true }).waitFor();
    assert.equal(await page.getByRole("switch").count(), 0, "no control creates access for everyone");
    assert.equal(await page.getByText("Everyone signed in").count(), 0);
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-access.png"), fullPage: true });

    step("Warning helper text is readable in both themes and the compact logo keeps its visible size");
    for (const mode of ["light", "dark"]) {
      await page.evaluate(mode => localStorage.setItem("hz-theme", mode), mode);
      await page.reload();
      await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
      await drawer().locator(".ant-typography-warning").first().waitFor();
      const ratios = await drawer().locator(".ant-typography-warning").evaluateAll(nodes => {
        const lum = color => {
          const rgb = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => v / 255);
          const lin = rgb.map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
          return lin[0] * .2126 + lin[1] * .7152 + lin[2] * .0722;
        };
        return nodes.map(node => {
          let parent = node, bg = "rgb(255,255,255)";
          while (parent) {
            const c = getComputedStyle(parent).backgroundColor;
            if (c !== "transparent" && c !== "rgba(0, 0, 0, 0)") { bg = c; break; }
            parent = parent.parentElement;
          }
          const a = lum(getComputedStyle(node).color), b = lum(bg);
          return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
        });
      });
      assert.ok(ratios.every(r => r >= 4.5), `${mode} warning contrast: ${ratios}`);
      await drawer().getByRole("button", { name: "Cancel" }).click();
    }
    await page.setViewportSize({ width: 873, height: 750 });
    await go("/agents");
    await page.locator(".topbar .brand-wordmark").waitFor();
    const logo = await page.locator(".topbar .brand-wordmark").evaluate(el => ({ width: el.clientWidth, height: el.clientHeight, fit: getComputedStyle(el).objectFit }));
    assert.deepEqual(logo, { width: 134, height: 30, fit: "none" });
    await page.getByRole("link", { name: "Manage access", exact: true }).first().waitFor();
    const darkContrast = await page.locator(".ant-btn-primary, .ant-tag-green, .ant-tag-success").evaluateAll(nodes => {
      const lum = color => color.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => v / 255)
        .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
        .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
      return nodes.filter(n => n.getBoundingClientRect().width).map(n => {
        const s = getComputedStyle(n), a = lum(s.color), b = lum(s.backgroundColor);
        return (Math.max(a,b) + .05) / (Math.min(a,b) + .05);
      });
    });
    assert.ok(darkContrast.length >= 2 && darkContrast.every(r => r >= 4.5), `Dark action/tag contrast: ${darkContrast}`);
    await page.screenshot({ path: path.join(shots, "hubzoid-console-dark-logo.png"), fullPage: true });
    await page.evaluate(() => localStorage.setItem("hz-theme", "light"));
    await page.setViewportSize({ width: 1440, height: 950 });
    await page.reload();

    // ---- stale hub responses --------------------------------------------------
    step("Switching agents never shows the previous agent's people while the new list loads");
    state.delays["/access"] = 700;
    await page.evaluate(() => {
      location.hash = "/agents/support/access";
    });
    await page.getByRole("heading", { name: "Access to Support Assistant" }).waitFor();
    assert.equal(await page.getByText("Priya Natarajan").count(), 0, "finance rows must vanish immediately");
    await page.getByRole("row").filter({ hasText: "Mei Lin Chen" }).getByText("Work tickets").waitFor();
    await page.getByRole("row").filter({ hasText: "Everyone signed in" }).getByText("Public", { exact: true }).waitFor();
    await page.getByRole("row").filter({ hasText: "Mei Lin Chen" }).getByText("Awaiting approval").waitFor();
    await page.getByText("Everyone signed in can use this agent").waitFor();
    delete state.delays["/access"];

    // ---- staged edit: cancel writes nothing ------------------------------------
    step("Editing then cancelling writes nothing and asks before discarding");
    await go("/agents/finance/access");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    await expand("Restricted tools");
    await drawer().getByRole("checkbox", { name: /Run payroll/ }).check();
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await modalTitle("Discard unsaved changes?").waitFor();
    await answer("Keep editing");
    assert.equal(await drawer().getByRole("checkbox", { name: /Run payroll/ }).isChecked(), true);
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await answer("Discard");
    await drawer().waitFor({ state: "hidden" });
    assert.equal(state.mutations.length, 0, "cancelling must not write");

    // ---- staged edit: review then save -------------------------------------------
    step("Review shows who, where, and each added or removed capability once; Save sends one request");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    const entry = drawer().getByRole("checkbox", { name: /Use this agent/ });
    assert.equal(await entry.isDisabled(), true, "entry is locked while other capabilities are selected");
    await drawer().getByText("Required", { exact: true }).waitFor();
    const entryHelp = drawer().getByRole("button", { name: "About Use this agent", exact: true });
    assert.equal(await entryHelp.getAttribute("aria-expanded"), "false");
    await entryHelp.click(); // a tap or click, not hover
    assert.equal(await entryHelp.getAttribute("aria-expanded"), "true");
    await (await controlled(entryHelp)).getByText("Required by the other selected capabilities", { exact: false }).waitFor();
    await expand("Restricted tools");
    await drawer().getByRole("checkbox", { name: /Manage invoices/ }).uncheck();
    await drawer().getByRole("checkbox", { name: /Run payroll/ }).check();
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByText("Review changes").first().waitFor();
    const review = drawer().locator(".access-review");
    await review.getByText("Priya Natarajan", { exact: true }).waitFor();
    await review.getByText(PRIYA, { exact: true }).waitFor();
    await review.getByText("Finance Assistant", { exact: true }).waitFor();
    await drawer().getByRole("list", { name: "Adding" }).getByText("Run payroll").waitFor();
    await drawer().getByRole("list", { name: "Removing" }).getByText("Manage invoices").waitFor();
    assert.equal(await review.getByRole("listitem").count(), 2, "each change is listed once");
    await drawer().getByText("This grants a sensitive capability").waitFor();
    assert.equal(await drawer().getByText(/Applied together|Allow Run payroll|Remove Manage invoices/).count(), 0, "no duplicate request list or implementation notes");
    for (const name of ["Cancel", "Back", "Save 2 changes"]) await drawer().getByRole("button", { name, exact: true }).waitFor();
    assert.equal(state.mutations.length, 0, "review must not write");
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-review.png"), fullPage: true });
    await drawer().getByRole("button", { name: "Save 2 changes" }).click();
    await saved();
    // One atomic request carrying the whole change set + the concurrency guard.
    assert.equal(state.mutations.length, 1);
    assert.equal(state.mutations[0].endpoint, "/access/apply");
    assert.equal(state.mutations[0].subject, PRIYA);
    assert.equal(state.mutations[0].hub, "finance");
    assert.equal(typeof state.mutations[0].expected_revision, "number");
    assert.deepEqual(state.mutations[0].operations, [
      { action: "revoke", permission: "invoices" },
      { action: "grant", permission: "payroll" },
    ]);
    await priyaRow.getByText("Run payroll", { exact: true }).waitFor();
    assert.equal(await priyaRow.getByText("Manage invoices", { exact: true }).count(), 0);
    state.mutations.length = 0;

    // ---- remove all access: single cascading request ---------------------------------
    step("Remove all access is confirmed as a consequential change and sends one cascading request");
    await page.getByRole("button", { name: "Edit access for daniel.okafor@example.org" }).click();
    await drawer().getByRole("button", { name: "Remove all access" }).click();
    await drawer().getByText("daniel.okafor@example.org will lose access to Finance Assistant").waitFor();
    assert.equal(await drawer().locator(".access-review li").count(), 1);
    await drawer().getByRole("button", { name: "Remove access", exact: true }).click();
    await saved();
    assert.equal(state.mutations.length, 1);
    assert.equal(state.mutations[0].endpoint, "/access/apply");
    assert.deepEqual(state.mutations[0].operations, [
      { action: "revoke", permission: "use_hub" },
    ]);
    await page.getByRole("row").filter({ hasText: "daniel.okafor" }).waitFor({ state: "detached" });
    state.mutations.length = 0;

    step("A legacy service identity keeps its label, grants and management controls");
    await page.getByRole("button", { name: "Edit access for workflow:monthly_close" }).click();
    await drawer().getByText("Legacy service identity", { exact: true }).waitFor();
    const legacyHelp = drawer().getByRole("button", { name: "About legacy service identities" });
    await legacyHelp.click();
    await (await controlled(legacyHelp)).getByText("Created before workflows ran as user accounts", { exact: false }).waitFor();
    assert.equal(await headerText("Restricted tools"), "Restricted tools · 1 selected", "its grants are kept and counted");
    await drawer().getByRole("button", { name: "Remove all access" }).click();
    await drawer().getByText("will lose access to Finance Assistant").waitFor();
    await drawer().locator(".access-review").getByText("Legacy service identity", { exact: true }).waitFor();
    const removing = drawer().getByRole("list", { name: "Removing" });
    await removing.getByText("Use this agent").waitFor();
    await removing.getByText("Read ledger").waitFor();
    assert.equal(await removing.getByRole("listitem").count(), 2, "each removed capability once");
    await drawer().getByRole("button", { name: "Back" }).click();
    await expand("Restricted tools");
    assert.equal(await drawer().getByRole("checkbox", { name: /Read ledger/ }).isChecked(), false);
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await answer("Discard");
    await drawer().waitFor({ state: "hidden" });
    assert.equal(state.mutations.length, 0);

    // ---- add a person: validation, implied entry, minimal requests ----------------------
    step("Adding a person asks only for an email, validates it, and lists each change once");
    await page.getByRole("button", { name: "Add person" }).click();
    assert.equal(await drawer().getByRole("radio").count(), 0, "no Person/Service switch");
    assert.equal(await drawer().getByText(/workflow:|Service identity/).count(), 0, "nothing suggests creating a workflow identity");
    const subject = drawer().getByRole("textbox", { name: "Email address" });
    await subject.fill("not an email");
    await expand("Restricted tools");
    await drawer().getByRole("checkbox", { name: /Read ledger/ }).check();
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByText("Enter a valid email address.", { exact: true }).waitFor();
    assert.equal(state.mutations.length, 0);
    await subject.fill("Ravi.Menon@example.org");
    await drawer().getByRole("button", { name: "Review changes" }).click();
    const adding = drawer().getByRole("list", { name: "Adding" });
    await adding.getByText("Read ledger").waitFor();
    assert.equal(await adding.getByText("Use this agent").count(), 1, "Use this agent is listed once");
    assert.equal(await adding.getByRole("listitem").count(), 2);
    await drawer().getByRole("button", { name: "Save 2 changes" }).click();
    await saved();
    assert.equal(state.mutations.length, 1);
    assert.equal(state.mutations[0].endpoint, "/access/apply");
    assert.equal(state.mutations[0].subject, "ravi.menon@example.org");
    assert.deepEqual(state.mutations[0].operations, [
      { action: "grant", permission: "ledger" },
    ], "use_hub is implied by the ledger grant: still one operation");
    await page.getByRole("row").filter({ hasText: "ravi.menon" }).getByText("Not signed up yet").waitFor();
    state.mutations.length = 0;

    step("New service identities can't be created; an existing legacy one opens for editing");
    await page.getByRole("button", { name: "Add person" }).click();
    const newSubject = drawer().getByRole("textbox", { name: "Email address" });
    await newSubject.fill("workflow:md:daily-notes");
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByText("New service identities can’t be added", { exact: false }).waitFor();
    assert.equal(await drawer().getByRole("button", { name: "Review changes" }).isDisabled(), true);
    assert.equal(state.mutations.length, 0, "nothing is created");
    await newSubject.fill("workflow:monthly_close");
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByText("already has access to Finance Assistant").waitFor();
    await drawer().getByText("Legacy service identity", { exact: true }).waitFor();
    await drawer().getByRole("button", { name: "Remove all access" }).waitFor();
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await drawer().waitFor({ state: "hidden" });
    assert.equal(state.mutations.length, 0);
    assert.equal(state.grants.some(([s]) => s === "workflow:md:daily-notes"), false);
    await page.reload();

    step("Optional groups start collapsed with counts; configuration reads apart from permission; toggles and help work by keyboard");
    // Temporary synthetic capabilities: one missing its setting, one included
    // with entry, and a grant whose capability no longer exists.
    const financeCatalog = state.catalogs.finance;
    state.catalogs.finance = [
      ...financeCatalog,
      perm("jev", "Ask Jev for decisions", "Use call_jev in chat for typed decisions from Jev.", false,
        { group: "tools", surfaces: ["chat"], status: "Jev key missing", available: false }),
      perm("email_me", "Email me", "Email a run's result to your own approved address.", false,
        { group: "tools", default: "included", status: "Email not configured", available: false }),
    ];
    const LENA = "lena.berg@example.org";
    state.identities[LENA] = { display: "Lena Berg", owui_id: "u_lena", pending: 0 };
    state.grants.push([LENA, "finance", USE_HUB_PERM], [LENA, "finance", "old_export"]);
    await page.reload();
    await page.getByRole("row").filter({ hasText: "Lena Berg" }).getByText("Old Export · no longer available").waitFor();
    await page.getByRole("button", { name: "Edit access for Lena Berg" }).click();
    assert.deepEqual(
      await drawer().locator(".capability-group-title").allTextContents(),
      ["Hub access", "Hubzoid tools", "Restricted tools", "Administration", "No longer available"],
      "groups in order; the empty Workflows group is hidden",
    );
    // Use this agent and removable leftovers stay in view; optional groups are closed.
    await drawer().getByRole("checkbox", { name: /Use this agent/ }).waitFor();
    const oldBox = drawer().getByRole("checkbox", { name: /Old Export/ });
    assert.equal(await oldBox.isChecked(), true);
    for (const name of ["Hubzoid tools", "Restricted tools", "Administration"]) {
      const toggle = group(name);
      assert.equal(await toggle.getAttribute("aria-expanded"), "false", `${name} starts collapsed`);
      const panel = await controlled(toggle);
      assert.equal(await panel.count(), 1, `${name} names the panel it controls`);
      assert.equal(await panel.isHidden(), true);
    }
    assert.equal(await group("Hub access").count(), 0, "Use this agent has no toggle");
    // Configuration is separate from permission: the included Email me is held
    // (with entry) but can't run, and the header says so while collapsed.
    assert.equal(await headerText("Hubzoid tools"), "Hubzoid tools · 1 not configured");
    // Real buttons: Enter and Space toggle a group.
    const tools = group("Hubzoid tools");
    await tools.focus();
    await page.keyboard.press("Enter");
    assert.equal(await tools.getAttribute("aria-expanded"), "true");
    const jevBox = drawer().getByRole("checkbox", { name: /Ask Jev for decisions/ });
    assert.equal(await jevBox.isDisabled(), false, "an unconfigured capability can still be granted");
    await drawer().getByText("Jev key missing", { exact: true }).waitFor();
    const emailBox = drawer().getByRole("checkbox", { name: /Email me/ });
    assert.equal(await emailBox.isChecked(), true, "included follows Use this agent");
    assert.equal(await emailBox.isDisabled(), true, "included has nothing to grant");
    const jevHelp = drawer().getByRole("button", { name: "About Ask Jev for decisions" });
    await jevHelp.focus();
    await page.keyboard.press("Enter");
    assert.equal(await jevHelp.getAttribute("aria-expanded"), "true");
    await (await controlled(jevHelp)).getByText("can’t run until an operator adds its settings", { exact: false }).waitFor();
    await jevBox.check();
    await tools.focus();
    await page.keyboard.press("Space");
    assert.equal(await tools.getAttribute("aria-expanded"), "false");
    assert.equal(await headerText("Hubzoid tools"), "Hubzoid tools · 1 selected · 2 not configured", "granted but unconfigured is shown as such");
    await page.keyboard.press("Space");
    assert.equal(await jevBox.isChecked(), true, "selections are kept across collapse and expand");
    await oldBox.uncheck();
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByText("can’t run until configured (Jev key missing)", { exact: false }).waitFor();
    await drawer().getByRole("button", { name: "Save 2 changes" }).click();
    await saved();
    assert.deepEqual(lastMutation().operations, [
      { action: "revoke", permission: "old_export" },
      { action: "grant", permission: "jev" },
    ]);
    // The fixture refuses what the server refuses: an included capability has no grant.
    await assert.rejects(fixture.handle("POST", "/access/apply", {}, {
      subject: LENA, hub: "finance", operations: [{ action: "grant", permission: "email_me" }],
    }), (e) => e.status === 422);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "Edit access for Lena Berg" }).click();
    await drawer().getByText("Hubzoid tools", { exact: true }).waitFor();
    let overflow = await drawer().locator(".ant-drawer-body").evaluate((el) => el.scrollWidth - el.clientWidth);
    assert.ok(overflow <= 0, `narrow drawer has no horizontal scroll (${overflow}px)`);
    await drawer().screenshot({ path: path.join(shots, "hubzoid-portal-capabilities-narrow.png") });
    await group("Hubzoid tools").click();
    await group("Restricted tools").click();
    await drawer().getByRole("checkbox", { name: /Ask Jev for decisions/ }).waitFor();
    overflow = await drawer().locator(".ant-drawer-body").evaluate((el) => el.scrollWidth - el.clientWidth);
    assert.ok(overflow <= 0, `narrow expanded drawer has no horizontal scroll (${overflow}px)`);
    await drawer().screenshot({ path: path.join(shots, "hubzoid-portal-capabilities-narrow-open.png") });
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await drawer().waitFor({ state: "hidden" });
    await page.setViewportSize({ width: 1440, height: 950 });
    state.catalogs.finance = financeCatalog;
    state.grants = state.grants.filter(([subject]) => subject !== LENA);
    delete state.identities[LENA];
    state.mutations.length = 0;
    await page.reload();

    step("A group holding a problem found at review opens by itself");
    await page.getByRole("button", { name: "Add person" }).click();
    await drawer().getByRole("textbox", { name: "Email address" }).fill("late.change@example.org");
    await expand("Restricted tools");
    await drawer().getByRole("checkbox", { name: /Run payroll/ }).check();
    await group("Restricted tools").click(); // close it again; the choice stays
    assert.equal(await headerText("Restricted tools"), "Restricted tools · 1 selected");
    // The agent drops the capability while the drawer is open.
    const catalogBefore = state.catalogs.finance;
    state.catalogs.finance = catalogBefore.filter((p) => p.permission !== "payroll");
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByText("One selected capability can’t be granted", { exact: false }).waitFor();
    assert.equal(await group("Restricted tools").getAttribute("aria-expanded"), "true", "the group with the problem opened");
    assert.equal(await headerText("Restricted tools"), "Restricted tools · 1 selected · needs attention");
    const payrollBox = drawer().getByRole("checkbox", { name: /Run payroll/ });
    assert.equal(await payrollBox.getAttribute("aria-invalid"), "true");
    await drawer().getByText("No longer available in this agent", { exact: false }).waitFor();
    assert.equal(state.mutations.length, 0);
    await payrollBox.uncheck();
    assert.equal(await drawer().getByText("No longer available in this agent", { exact: false }).count(), 0);
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByRole("list", { name: "Adding" }).getByText("Use this agent").waitFor();
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await answer("Discard");
    await drawer().waitFor({ state: "hidden" });
    state.catalogs.finance = catalogBefore;
    assert.equal(state.mutations.length, 0);

    step("Adding someone who already has access switches to editing their current access");
    await page.getByRole("button", { name: "Add person" }).click();
    await drawer().getByRole("textbox", { name: "Email address" }).fill(PRIYA);
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByText("already has access to Finance Assistant").waitFor();
    assert.equal(await headerText("Restricted tools"), "Restricted tools · 2 selected");
    await expand("Restricted tools");
    assert.equal(await drawer().getByRole("checkbox", { name: /Run payroll/ }).isChecked(), true);
    assert.equal(state.mutations.length, 0);
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await drawer().waitFor({ state: "hidden" });

    step("Granting to a blocked person surfaces the server's refusal with context");
    await page.getByRole("button", { name: "Add person" }).click();
    await drawer().getByRole("textbox", { name: "Email address" }).fill("tomas.herrera@example.org");
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByRole("button", { name: "Save change" }).click();
    await drawer().getByText("Nothing was saved").waitFor();
    await drawer().getByText("Reactivate this user before granting access").waitFor();
    await drawer().getByRole("button", { name: /Done/ }).click();
    state.mutations.length = 0;

    // ---- uncertain save: server error can't be confirmed --------------------------
    step("An uncertain (5xx) save says so honestly and reloads the real state");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    await expand("Restricted tools");
    await drawer().getByRole("checkbox", { name: /Manage invoices/ }).check();
    await drawer().getByRole("checkbox", { name: /Read ledger/ }).uncheck();
    await drawer().getByRole("button", { name: "Review changes" }).click();
    state.failNext = {
      match: (endpoint) => endpoint === "/access/apply",
      status: 503,
      detail: "Access store temporarily unavailable",
    };
    await drawer().getByRole("button", { name: "Save 2 changes" }).click();
    // 5xx is not a confirmed no-op — the UI must not claim nothing was saved.
    await drawer().getByText("Couldn’t confirm whether the changes were saved").waitFor();
    await drawer().getByText("Access store temporarily unavailable").waitFor();
    await drawer().locator(".access-review li[data-status=unknown]").first().waitFor();
    assert.equal(await drawer().getByText("No changes were saved").count(), 0, "must not falsely claim nothing saved");
    assert.equal(state.mutations.length, 1, "one atomic request");
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-uncertain-save.png"), fullPage: true });
    await drawer().getByRole("button", { name: /Done/ }).click();
    await drawer().waitFor({ state: "hidden" });
    // The fixture rolled back, so the reloaded state is unchanged.
    await priyaRow.getByText("Read ledger", { exact: true }).waitFor();
    assert.equal(await priyaRow.getByText("Manage invoices", { exact: true }).count(), 0, "the unconfirmed grant did not apply");
    state.mutations.length = 0;

    // ---- definite (4xx) save failure: nothing saved -------------------------------
    step("A definite (4xx) save failure states nothing was saved");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    await expand("Restricted tools");
    await drawer().getByRole("checkbox", { name: /Manage invoices/ }).check();
    await drawer().getByRole("button", { name: "Review changes" }).click();
    state.failNext = {
      match: (endpoint) => endpoint === "/access/apply",
      status: 409,
      detail: "Access changed since you loaded it — reload and review.",
    };
    await drawer().getByRole("button", { name: "Save change" }).click();
    await drawer().getByText("No changes were saved").waitFor();
    await drawer().locator(".access-review li[data-status=failed]").first().waitFor();
    await drawer().getByRole("button", { name: /Done/ }).click();
    await drawer().waitFor({ state: "hidden" });
    state.mutations.length = 0;

    // ---- failed recovery refresh keeps the editor locked (review #2) -----------------------
    step("A failed post-save refresh keeps the access editor locked until a successful retry");
    const priyaRow2 = page.getByRole("row").filter({ hasText: "Priya Natarajan" });
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    await expand("Restricted tools");
    await drawer().getByRole("checkbox", { name: /Read ledger/ }).uncheck();
    await drawer().getByRole("button", { name: "Review changes" }).click();
    // The save itself succeeds; the recovery refresh (GET /access) then fails once.
    state.failNextGet = { endpoint: "/access", status: 503, detail: "Access store temporarily unavailable" };
    await drawer().getByRole("button", { name: "Save change" }).click();
    await drawer().waitFor({ state: "hidden" });
    // Editor is LOCKED: the list is replaced by an error + retry, with no editable controls.
    await page.getByText("Couldn’t load this view").waitFor();
    await page.getByText("Access store temporarily unavailable").waitFor();
    assert.equal(await page.getByRole("button", { name: "Add person" }).count(), 0, "no Add while stale");
    assert.equal(await page.getByRole("button", { name: /^Edit access for/ }).count(), 0, "no rows editable while stale");
    // A successful retry unlocks the editor and reflects the applied change.
    await page.getByRole("button", { name: "Try again" }).click();
    await page.getByRole("button", { name: "Add person" }).waitFor();
    assert.equal(await priyaRow2.getByText("Read ledger", { exact: true }).count(), 0, "the revoke did apply");
    // Restore Priya's ledger grant for later steps.
    state.grants.push([PRIYA, "finance", "ledger"]);
    state.revision += 1;
    state.mutations.length = 0;

    // ---- concurrency: a change since load is refused --------------------------------------
    step("An edit built on stale access is refused when another admin changed it first");
    await page.getByRole("button", { name: "Add person" }).click();
    await drawer().getByRole("textbox", { name: "Email address" }).fill("concurrent.user@example.org");
    await expand("Restricted tools");
    await drawer().getByRole("checkbox", { name: /Read ledger/ }).check();
    await drawer().getByRole("button", { name: "Review changes" }).click();
    state.revision += 1; // another administrator changed access after this drawer loaded
    await drawer().getByRole("button", { name: "Save 2 changes" }).click();
    await drawer().getByText("Nothing was saved").waitFor();
    await drawer().getByText("Access changed since you loaded it", { exact: false }).waitFor();
    await drawer().getByRole("button", { name: /Done/ }).click();
    await drawer().waitFor({ state: "hidden" });
    assert.equal(state.grants.some(([s]) => s === "concurrent.user@example.org"), false, "nothing was written");
    state.mutations.length = 0;

    // ---- navigation guards --------------------------------------------------------------
    step("Leaving with a dirty draft (back button, typed URL) asks first; keeping editing restores the URL");
    await go("/people");
    await page.getByRole("heading", { name: "People", exact: true }).waitFor();
    await go("/agents/finance/access");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    await expand("Restricted tools");
    await drawer().getByRole("checkbox", { name: /Manage invoices/ }).check();
    await page.goBack();
    await modalTitle("Leave without saving?").waitFor();
    await answer("Keep editing");
    assert.equal(await hash(), "#/agents/finance/access");
    assert.equal(await drawer().getByRole("checkbox", { name: /Manage invoices/ }).isChecked(), true);
    // The drawer mask covers the sidebar, so leaving means back/forward or a typed URL.
    await page.evaluate(() => {
      location.hash = "/people";
    });
    await answer("Discard and leave");
    await page.getByRole("heading", { name: "People", exact: true }).waitFor();
    assert.equal(await hash(), "#/people");
    assert.equal(state.mutations.length, 0);

    step("Navigation is refused while a save is in flight");
    await go("/agents/finance/access");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    await expand("Restricted tools");
    await drawer().getByRole("checkbox", { name: /Manage invoices/ }).check();
    await drawer().getByRole("button", { name: "Review changes" }).click();
    state.delays["/access/apply"] = 900;
    await drawer().getByRole("button", { name: "Save change" }).click();
    await drawer().getByRole("button", { name: /Saving…/ }).waitFor();
    await page.evaluate(() => {
      location.hash = "/people";
    });
    await page.getByText("Wait for the current save to finish").waitFor();
    assert.equal(await hash(), "#/agents/finance/access");
    await saved();
    delete state.delays["/access/apply"];
    state.mutations.length = 0;

    // ---- everyone signed in: shown, never created, removal confirmed ----------------------
    step("Everyone signed in is shown with a confirmed Remove, and can never be granted");
    await go("/agents/support/access");
    const everyoneRow = page.getByRole("row").filter({ hasText: "Everyone signed in" });
    await everyoneRow.getByText("Use this agent", { exact: true }).waitFor();
    assert.equal(await everyoneRow.getByRole("button", { name: /^Edit access/ }).count(), 0, "not editable");
    await everyoneRow.getByRole("button", { name: "Remove access for everyone signed in" }).click();
    await modalTitle("Remove access for everyone signed in to Support Assistant?").waitFor();
    await modal().getByText(/\d+ chat accounts? opens? Support Assistant only through this/).waitFor();
    await answer("Cancel");
    assert.equal(state.mutations.length, 0, "cancel writes nothing");
    await page.setViewportSize({ width: 390, height: 844 });
    await everyoneRow.getByRole("button", { name: "Remove access for everyone signed in" }).click();
    await modalTitle("Remove access for everyone signed in to Support Assistant?").waitFor();
    await page.waitForTimeout(300);
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-remove-everyone-narrow.png") });
    await answer("Remove access for everyone");
    await page.getByText("Support Assistant is no longer open to everyone signed in.").waitFor();
    assert.deepEqual(lastMutation(), { endpoint: "/access/revoke", subject: "*", hub: "support", permission: "use_hub" });
    assert.equal(await page.getByRole("row").filter({ hasText: "Everyone signed in" }).count(), 0);
    await page.setViewportSize({ width: 1440, height: 950 });
    // The server refuses to create it again, for an organization administrator too.
    await assert.rejects(fixture.handle("POST", "/access/grant", {}, { subject: "*", hub: "support", permission: "use_hub" }),
      (e) => e.status === 403);
    state.grants.push([EVERYONE_SUBJECT, "support", USE_HUB_PERM]); // restore the carried-over grant
    state.mutations.length = 0;
    await go("/agents/finance/access");

    // ---- legacy (un-migrated) hub is read-only in the dashboard -------------------------
    step("A legacy agent shows access read-only (managed in the chat app), with edits disabled");
    await go("/agents/itops/access"); // itops is not authoritative in the fixture
    await page.getByText("access is managed in the chat app", { exact: false }).waitFor();
    assert.equal(await page.getByRole("button", { name: "Add person" }).isDisabled(), true,
      "legacy hub must not offer Add person");
    assert.equal(await page.getByRole("switch").count(), 0);
    assert.equal(state.mutations.length, 0);

    // ---- runs & schedules ---------------------------------------------------------------
    step("Runs & schedules explains workflow state, opens runs and run details with steps");
    await go("/agents/finance/runs");
    await page.getByRole("heading", { name: "Workflows in Finance Assistant" }).waitFor();
    const close = page.getByRole("row").filter({ hasText: "monthly_close" });
    await close.getByText("Scheduled", { exact: true }).waitFor();
    await close.getByText("Asia/Kolkata").waitFor();
    await page.getByRole("row").filter({ hasText: "reissue_invoice" }).getByText("No schedule; runs only when started explicitly.").waitFor();
    await page.getByRole("row").filter({ hasText: "reissue_invoice" }).getByText("Manual", { exact: true }).waitFor();
    await close.getByRole("link", { name: "View runs" }).click();
    await page.getByRole("heading", { name: /Runs of monthly_close/ }).waitFor();
    await page.getByRole("row").filter({ hasText: "mc-2026-08-01" }).getByText("Failed").waitFor();
    await page.getByRole("link", { name: "mc-2026-09-01" }).click();
    await page.getByText("Closed August 2026: 412 entries reconciled").waitFor();
    await page.getByText("Succeeded").first().waitFor();
    await page.getByText("reconcile", { exact: true }).waitFor();
    assert.equal(await hash(), "#/agents/finance/runs/monthly_close/mc-2026-09-01");
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-run.png"), fullPage: true });
    await go("/agents/finance/runs/monthly_close/mc-2026-08-01");
    await page.getByText("The run failed").waitFor();
    await page.getByText("LedgerLockedError: period 2026-07 is locked").first().waitFor();
    await go("/agents/finance/runs/monthly_close/does-not-exist");
    await page.getByText("Run not found").waitFor();
    await go("/agents/itops/runs");
    await page.getByText("Scheduler not running", { exact: false }).first().waitFor();
    await page.getByText("2 scheduled runs missed while the scheduler was down", { exact: false }).waitFor();
    await go("/agents/support/runs/ticket_digest");
    await page.getByText("No recorded runs of ticket_digest yet.").waitFor();

    // ---- cross-agent runs ----------------------------------------------------------------
    step("Runs across agents: cross-agent list, ordered, filtered before paging, URL-persisted");
    await go("/runs");
    await page.getByRole("heading", { name: "Runs across your agents" }).waitFor();
    // Runs from more than one agent are shown together, each labelled by agent.
    await page.getByRole("row").filter({ hasText: "mc-2026-09-01" }).getByText("Finance Assistant").waitFor();
    await page.getByRole("row").filter({ hasText: "td-2026-09-15" }).getByText("Support Assistant").waitFor();
    await page.getByRole("row").filter({ hasText: "pa-2026-09-17" }).getByText("IT Ops Assistant").waitFor();
    // Status filter is applied server-side (before pagination): only failures remain.
    await page.getByRole("combobox", { name: "Status" }).click();
    await page.locator(".ant-select-item-option").filter({ hasText: "Failed" }).click();
    await page.waitForFunction(() =>
      ![...document.querySelectorAll("tbody tr")].some((tr) => (tr.textContent || "").includes("mc-2026-09-01")),
    );
    await page.getByRole("row").filter({ hasText: "pa-2026-09-17" }).waitFor(); // itops ERROR survives
    await page.getByRole("row").filter({ hasText: "mc-2026-08-01" }).waitFor(); // finance ERROR survives
    assert.ok((await hash()).includes("status=failed"), await hash());
    // Narrow to one agent as well; the URL carries both filters.
    await page.getByRole("combobox", { name: "Agent" }).click();
    await page.locator(".ant-select-item-option").filter({ hasText: "Finance Assistant" }).click();
    await page.waitForFunction(() =>
      ![...document.querySelectorAll("tbody tr")].some((tr) => (tr.textContent || "").includes("pa-2026-09-17")),
    );
    await page.getByRole("row").filter({ hasText: "mc-2026-08-01" }).waitFor();
    assert.ok((await hash()).includes("agent=finance"), await hash());
    // Filters survive a full refresh.
    await page.reload();
    await page.getByRole("row").filter({ hasText: "mc-2026-08-01" }).waitFor();
    await page.waitForFunction(() =>
      ![...document.querySelectorAll("tbody tr")].some((tr) => (tr.textContent || "").includes("pa-2026-09-17")),
    );
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-runs.png"), fullPage: true });
    // Opening a run routes into its agent's run detail (with steps).
    await page.getByRole("link", { name: "mc-2026-08-01" }).click();
    assert.equal(await hash(), "#/agents/finance/runs/monthly_close/mc-2026-08-01");
    await page.getByText("The run failed").waitFor();
    // Back restores the filtered cross-agent view.
    await page.goBack();
    await page.getByRole("row").filter({ hasText: "mc-2026-08-01" }).waitFor();
    // Reset clears every filter.
    await page.getByRole("button", { name: "Reset filters" }).first().click();
    await page.getByRole("row").filter({ hasText: "pa-2026-09-17" }).waitFor();
    assert.ok(!(await hash()).includes("status="), await hash());

    step("Runs: a filter combination with no matches shows a distinct empty state + reset");
    await page.getByRole("combobox", { name: "Agent" }).click();
    await page.locator(".ant-select-item-option").filter({ hasText: "Support Assistant" }).click();
    await page.getByRole("combobox", { name: "Status" }).click();
    await page.locator(".ant-select-item-option").filter({ hasText: "Running" }).click();
    await page.getByText("No runs match these filters.").waitFor(); // support has no running run
    await page.locator(".ant-table-placeholder").getByRole("button", { name: "Reset filters" }).click();
    await page.getByRole("row").filter({ hasText: "pa-2026-09-17" }).waitFor();

    // Auto-refresh is toggleable and URL-persisted; deep behaviour is exercised
    // deterministically on a controlled clock below.
    await page.locator(".ant-checkbox-wrapper").filter({ hasText: "Auto-refresh" }).click();
    assert.ok((await hash()).includes("auto=1"), await hash());
    await page.locator(".ant-checkbox-wrapper").filter({ hasText: "Auto-refresh" }).click();
    assert.ok(!(await hash()).includes("auto=1"), await hash());

    step("Runs auto-refresh (controlled clock): fires once per interval, slides a live window, recovers, stops on unmount");
    const clockCtx = await browser.newContext({ viewport: { width: 1440, height: 950 } });
    const cpage = await clockCtx.newPage();
    const cerrors = [];
    cpage.on("pageerror", (e) => cerrors.push(String(e)));
    cpage.on("console", (m) => {
      if (m.type() === "error" && !/Failed to load resource/.test(m.text())) cerrors.push(m.text());
    });
    let runsGets = 0;
    const sinces = [];
    const untils = [];
    await clockCtx.route(`${ORIGIN}/**`, serve((url) => {
      if (url.pathname === "/portal/api/runs") {
        runsGets++;
        sinces.push(url.searchParams.get("since"));
        untils.push(url.searchParams.get("until"));
      }
    }));
    // Anchor the fake clock to real "now" so the synthetic runs (timestamped relative
    // to the fixture's real Date.now()) fall inside the windows we build below.
    const base = Date.now();
    await cpage.clock.install({ time: new Date(base) });

    // (a) Fires exactly once per interval (the in-flight guard prevents overlap).
    await cpage.goto(`${ORIGIN}/portal/#/runs?auto=1`);
    await cpage.getByRole("heading", { name: "Runs across your agents" }).waitFor();
    await cpage.getByRole("row").filter({ hasText: "ri-2026-09-18" }).waitFor();
    let n = runsGets;
    let resP = cpage.waitForResponse((x) => x.url().includes("/portal/api/runs"));
    await cpage.clock.runFor(11000);
    await resP;
    assert.equal(runsGets, n + 1, "exactly one background request per interval (no overlap)");

    // (b) A failed same-query refresh keeps the rows with a note (read-only recovery),
    //     rather than dropping to an error screen.
    state.failNextGet = { endpoint: "/runs", status: 503, detail: "Run history unavailable" };
    resP = cpage.waitForResponse((x) => x.url().includes("/portal/api/runs"));
    await cpage.clock.runFor(11000);
    await resP;
    await cpage.getByText("Couldn’t refresh just now", { exact: false }).waitFor();
    await cpage.getByRole("row").filter({ hasText: "ri-2026-09-18" }).waitFor();

    // (c) A live relative window advances its cutoff on each refresh.
    await cpage.goto(`${ORIGIN}/portal/#/runs?auto=1&range=1d`);
    await cpage.getByRole("row").filter({ hasText: "ri-2026-09-18" }).waitFor();
    const since1 = sinces[sinces.length - 1];
    assert.ok(since1, "a relative window sends a since cutoff");
    await cpage.clock.setSystemTime(new Date(base + 90 * 60 * 1000));
    resP = cpage.waitForResponse((x) => x.url().includes("/portal/api/runs"));
    await cpage.clock.runFor(11000);
    await resP;
    const since2 = sinces[sinces.length - 1];
    assert.ok(since2 > since1, `live window cutoff must advance (${since1} -> ${since2})`);

    // (d) Leaving the screen clears the interval — no further /runs calls.
    await cpage.goto(`${ORIGIN}/portal/#/agents`);
    await cpage.getByRole("heading", { name: "Agents", level: 1 }).waitFor();
    const afterLeave = runsGets;
    await cpage.clock.runFor(35000);
    assert.equal(runsGets, afterLeave, "auto-refresh interval must stop when the Runs screen unmounts");

    // (e) A FIXED window (auto off) freezes to absolute since/until and RETAINS them
    //     across a reload after the clock advances — reproducing a shared-link reopen.
    await cpage.clock.setSystemTime(new Date(base + 3 * 60 * 60 * 1000));
    await cpage.goto(`${ORIGIN}/portal/#/runs?range=1d`); // fixed: frozen on open
    await cpage.getByRole("row").filter({ hasText: "ri-2026-09-18" }).waitFor();
    await cpage.waitForFunction(
      () => location.hash.includes("since=") && location.hash.includes("until="),
    );
    const fixedSince = sinces[sinces.length - 1];
    const fixedUntil = untils[untils.length - 1];
    assert.ok(fixedSince && fixedUntil, "a fixed window sends absolute since & until");
    // Advance the clock two more hours and reload (reopening the same shared link).
    await cpage.clock.setSystemTime(new Date(base + 5 * 60 * 60 * 1000));
    resP = cpage.waitForResponse((x) => x.url().includes("/portal/api/runs"));
    await cpage.reload();
    await resP;
    await cpage.getByRole("row").filter({ hasText: "ri-2026-09-18" }).waitFor();
    assert.equal(sinces[sinces.length - 1], fixedSince, "fixed since retained after clock+reload");
    assert.equal(untils[untils.length - 1], fixedUntil, "fixed until retained (upper bound stays put)");

    assert.deepEqual(cerrors, [], "no console/page errors on the clock page");
    await clockCtx.close();

    // ---- activity -----------------------------------------------------------------------
    step("Activity reads as sentences with names, capabilities and agents; filters work");
    await go("/agents/finance/activity");
    await page.getByText("allowed").first().waitFor();
    const change = page.getByRole("row").filter({ hasText: "Aisha Rahman" }).filter({ hasText: "Read ledger" }).first();
    await change.getByText("Priya Natarajan").waitFor();
    await change.getByText("Finance Assistant").waitFor();
    // Case-sensitive: the sentence verb is lowercase "allowed"; the status tag is "Allowed".
    await change.getByText("allowed", { exact: true }).waitFor();
    // The saves made earlier in this run are recorded with the viewer as actor.
    await page.getByRole("row").filter({ hasText: "Sam Whitfield" }).filter({ hasText: "Run payroll" }).first().waitFor();
    // antd Segmented hides the radio input; click the visible label.
    await page.locator(".ant-segmented-item-label", { hasText: "Tool decisions" }).click();
    const denied = page.getByRole("row").filter({ hasText: "payroll_run" });
    await denied.getByText("Tomás Herrera").waitFor();
    await denied.getByText("was denied").waitFor();
    await denied.getByText("They do not have the permission this tool requires.").waitFor();
    await page.getByRole("row").filter({ hasText: "ledger_read" }).filter({ hasText: "Priya" }).getByText("used").waitFor();
    // Outcome filter (server-side, before pagination): only denials remain.
    await page.getByRole("combobox", { name: "Outcome" }).click();
    await page.locator(".ant-select-item-option").filter({ hasText: "Denied" }).click();
    await page.getByText("was denied").first().waitFor();
    // Wait for the filtered refetch to drop the allow-only rows (Priya's ledger_read).
    await page.waitForFunction(
      () =>
        ![...document.querySelectorAll("tbody tr")].some((tr) =>
          (tr.textContent || "").includes("Priya Natarajan"),
        ),
    );
    await page.getByText("Someone not signed in").waitFor();
    // The outcome filter is persisted in the URL.
    assert.ok((await hash()).includes("outcome=deny"), await hash());

    // Debounced text filters keep focus (keyed by a reset token, not their value) and
    // each has its OWN timer, so tool and channel don't cancel each other's commit.
    step("Activity text filters keep focus while typing and don't cancel each other");
    const toolBox = page.getByRole("textbox", { name: "Filter by tool" });
    await toolBox.click();
    await toolBox.pressSequentially("ledger_read");
    const chanBox = page.getByRole("textbox", { name: "Filter by channel" });
    await chanBox.click();
    await chanBox.pressSequentially("openwebui");
    // Both debounced commits land in the URL (independent timers).
    await page.waitForFunction(
      () => location.hash.includes("tool=ledger_read") && location.hash.includes("channel=openwebui"),
    );
    // Focus stayed in the channel input — the debounced commit did not remount it.
    assert.equal(
      await page.evaluate(() => document.activeElement && document.activeElement.getAttribute("aria-label")),
      "Filter by channel",
      "typing a filter must not steal focus",
    );
    await page.getByRole("button", { name: "Reset filters" }).first().click();
    await page.waitForFunction(
      () => !location.hash.includes("tool=") && !location.hash.includes("channel="),
    );

    // Event details: a keyboard-reachable row action opens the full record.
    await page.getByRole("row").filter({ hasText: "payroll_run" }).getByRole("button", { name: "Details" }).first().click();
    await drawer().getByText("Event details").waitFor();
    await drawer().getByText("payroll_run").first().waitFor();
    await drawer().getByText("They do not have the permission this tool requires.").waitFor();
    await page.keyboard.press("Escape");
    await go("/activity");
    await page.getByRole("heading", { name: "Activity across your agents" }).waitFor();
    await page.getByText("made").first().waitFor();
    await page.getByRole("row").filter({ hasText: "an organization administrator" }).getByText("Sam Whitfield").waitFor();
    await page.getByRole("row").filter({ hasText: "blocked" }).getByText("Tomás Herrera").waitFor();
    await page.getByRole("row").filter({ hasText: "Everyone signed in" }).getByText("Support Assistant").first().waitFor();
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-activity.png"), fullPage: true });

    // ---- people -----------------------------------------------------------------------------
    step("People shows identity vs account state, details drawer and confirmed admin actions");
    state.visibility = { state: "error", error: "ConnectError: visibility sync failed", updated: Date.now() / 1000 - 120 };
    await go("/people");
    await page.getByText("Recent access changes haven’t reached the chat app yet").waitFor();
    for (const [who, status] of [
      ["Priya Natarajan", "Active"],
      ["daniel.okafor@example.org", "Not signed up yet"],
      ["Mei Lin Chen", "Awaiting approval"],
      ["Tomás Herrera", "Blocked"],
      ["workflow:monthly_close", "Legacy service identity"],
    ])
      await page.getByRole("row").filter({ hasText: who }).getByText(status, { exact: true }).waitFor();
    await page.getByRole("row").filter({ hasText: "Aisha Rahman" }).getByText("Org admin").waitFor();
    await page.getByRole("button", { name: "Try again now" }).click();
    await page.getByText("Chat app visibility is in sync.").waitFor();
    assert.deepEqual(lastMutation(), { endpoint: "/sync" });
    state.mutations.length = 0;
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-people.png"), fullPage: true });

    await page.getByRole("link", { name: "Details for Tomás Herrera" }).click();
    assert.equal(await hash(), "#/people/tomas.herrera%40example.org");
    await drawer().getByText("All access was removed when they were blocked.").waitFor();
    await drawer().getByRole("button", { name: "Reactivate" }).click();
    await modalTitle("Reactivate Tomás Herrera?").waitFor();
    assert.equal(state.mutations.length, 0);
    await answer("Reactivate");
    await page.getByText("Tomás Herrera is active again.").waitFor();
    assert.deepEqual(lastMutation(), { endpoint: "/people/block", subject: "tomas.herrera@example.org", suspended: false });
    await drawer().getByText("Active", { exact: true }).waitFor();
    await drawer().getByRole("button", { name: "Block access" }).waitFor();
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-person.png"), fullPage: true });
    await page.goBack();
    await drawer().waitFor({ state: "hidden" });
    assert.equal(await hash(), "#/people");
    state.mutations.length = 0;

    step("An unavailable chat account is not reactivable, but CAN be explicitly offboarded");
    state.identities["gone.user@example.org"] = { display: "Gone User", owui_id: "u_gone", pending: 0 };
    state.grants.push(["gone.user@example.org", "finance", "use_hub"]);
    state.grants.push(["gone.user@example.org", "finance", "ledger"]);
    state.unavailable.add("gone.user@example.org");
    await go("/people/gone.user%40example.org");
    await drawer().getByText("Gone User").waitFor();
    await drawer().getByText("chat account is unavailable", { exact: false }).waitFor();
    // Reactivate is still not offered (the account is gone from the chat app)...
    assert.equal(await drawer().getByRole("button", { name: "Reactivate" }).count(), 0, "an unavailable account can't be reactivated here");
    // ...but Block IS offered so an admin can explicitly offboard and drop retained grants.
    await drawer().getByRole("button", { name: "Block access" }).click();
    await modalTitle("Block Gone User?").waitFor();
    await answer("Block access");
    await page.getByText("Gone User is blocked.").waitFor();
    assert.deepEqual(lastMutation(), { endpoint: "/people/block", subject: "gone.user@example.org", suspended: true });
    // The offboard removed the retained direct grants.
    assert.equal(state.grants.filter(([s]) => s === "gone.user@example.org").length, 0, "retained grants removed on offboard");
    await page.goBack();
    await drawer().waitFor({ state: "hidden" });
    state.unavailable.delete("gone.user@example.org");
    state.suspended.delete("gone.user@example.org");
    state.grants = state.grants.filter(([s]) => s !== "gone.user@example.org");
    delete state.identities["gone.user@example.org"];
    state.mutations.length = 0;

    step("People filters narrow the list, persist in the URL, and reset");
    await go("/people");
    await page.getByRole("combobox", { name: "Role" }).click();
    await page.locator(".ant-select-item-option").filter({ hasText: "Administrator" }).click();
    const noDaniel = () =>
      page.waitForFunction(
        () =>
          ![...document.querySelectorAll("tbody tr")].some((tr) =>
            (tr.textContent || "").includes("daniel.okafor"),
          ),
      );
    await noDaniel(); // filtered refetch removed the non-admin (beyond the first page too)
    await page.getByRole("row").filter({ hasText: "Aisha Rahman" }).waitFor();
    assert.ok((await hash()).includes("role=admin"), await hash());
    await page.reload(); // URL-persisted filter survives a refresh
    await noDaniel();
    await page.getByRole("row").filter({ hasText: "Aisha Rahman" }).waitFor();
    await page.getByRole("button", { name: "Reset filters" }).first().click();
    await page.getByRole("row").filter({ hasText: "daniel.okafor" }).waitFor();

    step("Person → Edit access opens that person's editor for the agent directly");
    await go(`/people/${encodeURIComponent(PRIYA)}`);
    await drawer().getByRole("link", { name: "Edit access" }).first().click();
    assert.ok((await hash()).startsWith("#/agents/finance/access"), await hash());
    await drawer().getByRole("heading", { name: "Capabilities" }).waitFor(); // editor opened…
    await drawer().getByText("Priya Natarajan").first().waitFor(); // …for this person
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await drawer().waitFor({ state: "hidden" });
    state.mutations.length = 0;

    step("Organization administrator rights are granted and protected from removing the last admin");
    await go(`/people/${encodeURIComponent(PRIYA)}`);
    await drawer().getByText("Regular access").waitFor();
    await drawer().getByRole("link", { name: "Edit access" }).first().waitFor();
    await drawer().getByText("Run payroll").waitFor();
    await drawer().getByRole("button", { name: "Make organization administrator" }).click();
    await answer("Make administrator");
    await page.getByText("Priya Natarajan is now an organization administrator.").waitFor();
    assert.deepEqual(lastMutation(), { endpoint: "/access/grant", subject: PRIYA, hub: "*", permission: "manage_access" });
    await drawer().getByText("Organization administrator", { exact: true }).waitFor();
    state.mutations.length = 0;
    // Make Priya the only admin left, then try to demote her.
    state.grants = state.grants.filter(([s, h, p]) => !(h === "*" && p === "manage_access" && s !== PRIYA));
    await go(`/people/${encodeURIComponent(PRIYA)}`);
    await drawer().getByRole("button", { name: "Remove administrator rights" }).click();
    await answer("Remove rights");
    await drawer().getByText("cannot remove the last org admin").waitFor();
    state.mutations.length = 0;

    // ---- deep links and recovery ------------------------------------------------------------
    step("Unknown routes and agents recover to a useful place");
    await go("/nowhere/at/all");
    await page.getByText("Page not found").waitFor();
    await page.getByRole("link", { name: "Back to agents" }).click();
    await page.getByRole("heading", { name: "Agents", level: 1 }).waitFor();
    await go("/agents/payroll-bot/access");
    await page.getByText("Agent not found").waitFor();
    await go("/agents/finance/settings");
    await page.getByText("has Access, Runs & schedules and Activity").waitFor();
    await go("/agents/finance/runs");
    // Target the agent's own Activity tab, not the org-level sidebar link.
    await page.locator(".ant-tabs").getByRole("link", { name: "Activity", exact: true }).click();
    assert.equal(await hash(), "#/agents/finance/activity");
    await page.goBack();
    await page.getByRole("heading", { name: "Workflows in Finance Assistant" }).waitFor();

    // ---- agent administrator (not org) ------------------------------------------------------
    step("An agent administrator sees only their agents and cannot touch admin or public access");
    // Restore Aisha's org-admin grant (an earlier step stripped all admins but
    // Priya) so this section can prove a hub admin cannot alter an org admin.
    if (!state.grants.some(([s, h, p]) => s === "aisha.rahman@example.org" && h === "*" && p === "manage_access"))
      state.grants.push(["aisha.rahman@example.org", "*", "manage_access"]);
    state.role = "hub";
    await page.reload(); // a role change is a fresh session; refetch /me and /hubs
    await go("/agents");
    await page.getByRole("link", { name: "Finance Assistant", exact: true }).waitFor();
    assert.equal(await page.getByRole("link", { name: "Support Assistant", exact: true }).count(), 0);
    await page.getByText("Agent administrator").waitFor();
    await go("/agents/finance/access");
    await page.getByRole("heading", { name: "Access to Finance Assistant" }).waitFor();
    // An agent administrator sees an existing Everyone grant read-only.
    state.grants.push([EVERYONE_SUBJECT, "finance", USE_HUB_PERM]);
    await page.reload();
    const readOnly = page.getByRole("row").filter({ hasText: "Everyone signed in" }).getByText("Read-only");
    await readOnly.hover();
    await page.getByRole("tooltip").filter({ hasText: "Only organization administrators can remove this." }).waitFor();
    assert.equal(await page.getByRole("button", { name: "Remove access for everyone signed in" }).count(), 0);
    state.grants = state.grants.filter(([s, h]) => !(s === EVERYONE_SUBJECT && h === "finance"));
    await page.reload();
    await page.getByRole("button", { name: "Edit access for Aisha Rahman" }).click();
    assert.equal(await headerText("Administration"), "Administration · 1 selected", "inherited rights are counted");
    await expand("Administration");
    assert.equal(await drawer().getByRole("checkbox", { name: /Manage access/ }).isDisabled(), true);
    assert.equal(await drawer().getByRole("checkbox", { name: /Use this agent/ }).isDisabled(), true);
    await drawer().getByText("Inherited", { exact: true }).first().waitFor();
    const inheritedHelp = drawer().getByRole("button", { name: "About Manage access", exact: true });
    await inheritedHelp.click();
    await (await controlled(inheritedHelp)).getByText("Held through organization administrator rights", { exact: false }).waitFor();
    assert.equal(await drawer().getByRole("button", { name: "Remove all access" }).count(), 0);
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await page.getByRole("button", { name: "Edit access for Finance Admin" }).click();
    await expand("Administration");
    const delegateHelp = drawer().getByRole("button", { name: "About Manage access", exact: true });
    await delegateHelp.click();
    await (await controlled(delegateHelp)).getByText("Only organization administrators can change this.", { exact: false }).waitFor();
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await go("/agents/support/access");
    await page.getByText("Agent not found").waitFor();
    await go(`/people/${encodeURIComponent(PRIYA)}`);
    await drawer().getByText("Access by agent").waitFor();
    assert.equal(await drawer().getByRole("button", { name: "Block access" }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "Refresh accounts" }).count(), 0);
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-agent-admin.png"), fullPage: true });
    state.mutations.length = 0;

    // ---- accounts and confirmations (stubbed API: the fixture predates them) ------------------
    step("A delegate adds an account: only their own capabilities are selectable; the password shows once");
    // The fixture's /me predates grantable/account fields; later-registered routes win.
    const meExtras = () =>
      state.role === "hub"
        ? { grantable: { finance: [USE_HUB_PERM] }, account_admin: false, can_create_accounts: true, accounts_configured: true }
        : { grantable: { finance: state.catalogs.finance.map((p) => p.permission), support: state.catalogs.support.map((p) => p.permission), itops: [] }, account_admin: true, can_create_accounts: true, accounts_configured: true };
    const meRoute = async (route) => {
      try {
        const base = await fixture.handle("GET", "/me", {}, undefined);
        return route.fulfill({ json: { ...base, ...meExtras() } });
      } catch (e) {
        return route.fulfill({ status: e.status || 500, json: { detail: e.detail || String(e) } });
      }
    };
    const accountCalls = [];
    const accountsRoute = async (route) => {
      const body = route.request().postDataJSON();
      accountCalls.push(body);
      if (body.email === PRIYA)
        return route.fulfill({ status: 409, json: { detail: "An account with this email already exists. Grant access instead.", code: "account_exists" } });
      const grants = {};
      for (const g of body.grants) (grants[g.hub] ??= []).push(g.permission);
      return route.fulfill({ json: { ok: true, subject: body.email, name: body.name, role: "user", grants, revision: 1 } });
    };
    await context.route(`${ORIGIN}/portal/api/me`, meRoute);
    await context.route(`${ORIGIN}/portal/api/accounts`, accountsRoute);
    await page.reload();
    await go("/people");
    await page.getByRole("button", { name: "Add account" }).click();
    await drawer().getByText("Initial access").waitFor();
    // Capabilities the delegate does not hold are shown but not selectable.
    assert.equal(await drawer().getByRole("checkbox", { name: /Run payroll/ }).isDisabled(), true);
    await drawer().getByText("Outside your access").first().waitFor();
    assert.equal(await drawer().getByRole("checkbox", { name: /Use this agent/ }).isDisabled(), false);
    await drawer().getByRole("textbox", { name: "Email address" }).fill("new.person@example.org");
    await drawer().getByRole("textbox", { name: "Name" }).fill("New Person");
    await drawer().getByRole("button", { name: "Generate" }).click();
    const generated = await page.locator("#account-password").inputValue();
    assert.ok(generated.length >= 16, "a generated password is filled in");
    await drawer().getByRole("checkbox", { name: /Use this agent/ }).check();
    await drawer().getByRole("button", { name: "Review" }).click();
    await drawer().getByText("Finance Assistant: Use this agent").waitFor();
    await drawer().getByRole("button", { name: "Create account" }).click();
    await drawer().getByText("New Person can now sign in as new.person@example.org").waitFor();
    assert.equal(await page.locator("#one-time-password").inputValue(), generated);
    assert.deepEqual(accountCalls[0], {
      email: "new.person@example.org", name: "New Person", password: generated,
      grants: [{ hub: "finance", permission: USE_HUB_PERM }],
    });
    await drawer().getByRole("button", { name: "Done" }).click();
    await drawer().waitFor({ state: "hidden" });

    step("An existing account is not duplicated; the drawer offers granting access instead");
    await page.getByRole("button", { name: "Add account" }).click();
    await drawer().getByRole("textbox", { name: "Email address" }).fill(PRIYA);
    await drawer().getByRole("textbox", { name: "Name" }).fill("Priya");
    await page.locator("#account-password").fill("Typed-Password-42");
    await drawer().getByRole("checkbox", { name: /Use this agent/ }).check();
    await drawer().getByRole("button", { name: "Review" }).click();
    await drawer().getByRole("button", { name: "Create account" }).click();
    await drawer().getByText("This person already has an account").waitFor();
    await drawer().getByRole("link", { name: "Grant access in Finance Assistant" }).waitFor();
    await drawer().getByRole("button", { name: "Done" }).click();
    await drawer().waitFor({ state: "hidden" });

    step("A change proposed from chat is confirmed on its own page, exactly as proposed");
    const now = Math.floor(Date.now() / 1000);
    const changeRequest = {
      id: "req_abcdefghijklmnopqrstu", status: "pending", kind: "access", hub: "finance",
      hub_name: "Finance Assistant", target: PRIYA, surface: "whatsapp", created: now - 60,
      expires: now + 840, decided: null, plan_hash: "plan-hash-1", result: null, problem: null,
      plan: { kind: "access", hub: "finance", subject: PRIYA, grant: ["invoices"], revoke: [] },
      summary: "For priya in finance: allow Manage invoices.",
      labels: Object.fromEntries(state.catalogs.finance.map((p) => [p.permission, p])),
      current: [USE_HUB_PERM, "ledger"],
    };
    const confirmCalls = [];
    const changeRoute = async (route) => {
      const u = new URL(route.request().url());
      if (route.request().method() === "GET") return route.fulfill({ json: changeRequest });
      confirmCalls.push({ path: u.pathname, body: route.request().postDataJSON() });
      changeRequest.status = u.pathname.endsWith("/confirm") ? "confirmed" : "rejected";
      return route.fulfill({ json: { id: changeRequest.id, status: changeRequest.status, result: {} } });
    };
    await context.route(`${ORIGIN}/portal/api/change-requests/**`, changeRoute);
    await go(`/confirm/${changeRequest.id}`);
    await page.getByRole("heading", { name: "Change access", level: 1 }).waitFor();
    await page.getByText("Proposed from WhatsApp", { exact: false }).waitFor();
    await page.getByText("Manage invoices", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Apply change" }).click();
    await page.getByText("Access updated").waitFor();
    assert.deepEqual(confirmCalls[0], {
      path: `/portal/api/change-requests/${changeRequest.id}/confirm`,
      body: { plan_hash: "plan-hash-1" },
    });
    assert.equal(await page.getByRole("button", { name: "Apply change" }).count(), 0);
    await context.unroute(`${ORIGIN}/portal/api/change-requests/**`, changeRoute);
    await context.unroute(`${ORIGIN}/portal/api/accounts`, accountsRoute);
    await context.unroute(`${ORIGIN}/portal/api/me`, meRoute);
    state.mutations.length = 0;

    // ---- mobile -------------------------------------------------------------------------------
    step("Mobile layout keeps navigation reachable and the editor usable");
    state.role = "org";
    await page.reload(); // back to a full org-admin session
    await page.setViewportSize({ width: 390, height: 844 });
    await go("/agents");
    await page.getByRole("list", { name: "Totals" }).getByText("Messages", { exact: true }).waitFor();
    assert.equal(await page.getByRole("listitem").count(), 5);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Mobile dashboard has no horizontal overflow");
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-overview-mobile.png"), fullPage: true });
    await go("/agents/finance/access");
    await page.getByRole("button", { name: "Open navigation" }).click();
    await page.getByRole("dialog").getByRole("link", { name: "People" }).waitFor();
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-mobile-nav.png") });
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    await expand("Restricted tools");
    await drawer().getByRole("checkbox", { name: /Read ledger/ }).waitFor();
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-mobile-editor.png") });
    await drawer().getByRole("button", { name: "Cancel" }).click();

    step("On a touch screen, groups and help open by tapping");
    const touchCtx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const tpage = await touchCtx.newPage();
    await touchCtx.route(`${ORIGIN}/**`, serve());
    await tpage.goto(`${ORIGIN}/portal/#/agents/finance/access`);
    await tpage.getByRole("button", { name: "Edit access for Priya Natarajan" }).tap();
    const tdrawer = tpage.locator(".ant-drawer-section[role=dialog]");
    const ttoggle = tdrawer.getByRole("button", { name: /^Restricted tools/ });
    await ttoggle.tap();
    assert.equal(await ttoggle.getAttribute("aria-expanded"), "true");
    const thelp = tdrawer.getByRole("button", { name: "About Read ledger" });
    await thelp.tap();
    assert.equal(await thelp.getAttribute("aria-expanded"), "true");
    await tpage.locator(`[id="${await thelp.getAttribute("aria-controls")}"]`).getByText("View accounting entries and balances.").waitFor();
    await touchCtx.close();
    await page.setViewportSize({ width: 1440, height: 950 });

    // ---- ordinary user -----------------------------------------------------------------------
    step("An ordinary user is turned away without seeing any administration UI");
    state.role = "user";
    await page.reload(); // fresh session as an ordinary (non-admin) user
    await go("");
    await page.getByText("Console access is not enabled for this account").waitFor();
    assert.equal(await page.getByRole("link", { name: "Agents" }).count(), 0);
    await page.getByRole("link", { name: "Go to the chat app" }).waitFor();

    // ---- OWUI navigation link reacts to SPA login/logout --------------------------------
    step("OWUI navigation link appears for an admin session and disappears after logout");
    const pyNav = fs.readFileSync(path.resolve(__dirname, "../../hubzoid/portal_navigation.py"), "utf8");
    const navMatch = pyNav.match(/SCRIPT = r'''\n([\s\S]*?)'''/);
    assert.ok(navMatch, "found the injected nav SCRIPT in portal_navigation.py");
    const navScript = navMatch[1];
    let navLoggedIn = true; // toggled to simulate SPA logout/login (no page reload)
    const navCtx = await browser.newContext();
    const navPage = await navCtx.newPage();
    await navCtx.route(`${ORIGIN}/**`, async (route) => {
      const u = new URL(route.request().url());
      if (u.pathname === "/hubzoid-portal-navigation.js")
        return route.fulfill({ body: navScript, contentType: "application/javascript" });
      if (u.pathname === "/portal/api/me")
        return navLoggedIn
          ? route.fulfill({ json: { subject: "root", org_admin: true, manageable: [] } })
          : route.fulfill({ status: 403, json: { detail: "sign in" } });
      if (u.pathname === "/owui-stub")
        return route.fulfill({
          contentType: "text/html",
          body: '<!doctype html><html><body><div id="app">chat</div><nav id="sidebar" style="width:240px"><div style="display:flex"><span role="button" aria-haspopup="true"><button aria-label="User menu">User</button></span></div></nav>'
            + '<script src="/hubzoid-portal-navigation.js" defer></script></body></html>',
        });
      return route.fulfill({ status: 404, body: "" });
    });
    await navPage.goto(`${ORIGIN}/owui-stub`);
    await navPage.locator("#hubzoid-manage-access").waitFor(); // shown for an admin session
    assert.equal(await navPage.locator('#sidebar #hubzoid-manage-access').count(), 1);
    assert.equal(await navPage.locator('#hubzoid-manage-access').getAttribute('title'), 'Hubzoid Admin Console — manage access and view workflow runs');
    assert.ok(await navPage.locator('#hubzoid-manage-access').evaluate(el => el.nextElementSibling.querySelector('button[aria-label="User menu"]') !== null));
    assert.equal(await navPage.locator('#hubzoid-manage-access span').isVisible(), true);
    // Sidebar replacement, as in OWUI's compact layout, must reattach one icon.
    await navPage.evaluate(() => {
      document.querySelector('#sidebar').outerHTML = '<nav id="sidebar" style="width:42px"><div style="display:flex"><span role="button" aria-haspopup="true"><button aria-label="User menu">User</button></span></div></nav>';
    });
    await navPage.locator('#sidebar #hubzoid-manage-access[data-compact]').waitFor();
    assert.equal(await navPage.locator('#hubzoid-manage-access span').isVisible(), false);
    assert.equal(await navPage.locator('#hubzoid-manage-access').count(), 1);
    await navPage.locator('#hubzoid-manage-access').focus();
    assert.equal(await navPage.evaluate(() => document.activeElement.id), 'hubzoid-manage-access');
    // SPA logout: /me now 403; an in-app navigation fires popstate (no reload).
    navLoggedIn = false;
    await navPage.evaluate(() => dispatchEvent(new PopStateEvent("popstate")));
    await navPage.locator("#hubzoid-manage-access").waitFor({ state: "detached" }); // removed
    // SPA login again: the link comes back on the next navigation.
    navLoggedIn = true;
    await navPage.evaluate(() => dispatchEvent(new PopStateEvent("popstate")));
    await navPage.locator("#hubzoid-manage-access").waitFor();
    await navCtx.close();

    assert.deepEqual(errors, [], "no console or page errors");
    console.log(`\nPASS: ${steps.length} journeys`);
  } catch (error) {
    if (diagnosticPage) {
      fs.writeFileSync(path.join(shots, "hubzoid-journey-failure.html"), await diagnosticPage.content());
      await diagnosticPage.screenshot({ path: path.join(shots, "hubzoid-journey-failure.png") });
    }
    throw error;
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(`\nFAIL at: ${steps[steps.length - 1]}\n`, error);
  process.exit(1);
});
