// Browser regression checks for the agent-first portal. They run the built
// assets (hubzoid/portal_dist) against the synthetic fixture in fixture.cjs
// via request interception: no server, no customer data, no live mutations.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createFixture } = require("./fixture.cjs");

const root = path.resolve(__dirname, "../../hubzoid/portal_dist");
const shots = process.env.PORTAL_SHOTS || "/private/tmp";
const ORIGIN = "http://hubzoid.test";
const PRIYA = "priya.natarajan@example.org";

const steps = [];
function step(name) {
  steps.push(name);
  console.log(`· ${name}`);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const fixture = createFixture();
  const { state } = fixture;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
    const page = await context.newPage();
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
    await context.route(`${ORIGIN}/**`, async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.startsWith("/portal/api")) {
        const endpoint = url.pathname.slice("/portal/api".length);
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
    });
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

    // ---- landing -----------------------------------------------------------
    step("Agents landing lists every agent with its access state and attention items");
    await go("");
    await page.getByRole("heading", { name: "Agents", level: 2 }).waitFor();
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
    await page.getByRole("heading", { name: "Who can use Finance Assistant" }).waitFor();
    const priyaRow = page.getByRole("row").filter({ hasText: "Priya Natarajan" });
    await priyaRow.getByText("Read ledger", { exact: true }).waitFor();
    await priyaRow.getByText("Manage invoices", { exact: true }).waitFor();
    await priyaRow.getByText("Active", { exact: true }).waitFor();
    await page.getByRole("row").filter({ hasText: "Aisha Rahman" }).getByText("Manage access · inherited").waitFor();
    await page.getByRole("row").filter({ hasText: "daniel.okafor" }).getByText("Not signed up yet").waitFor();
    await page.getByRole("row").filter({ hasText: "monthly_close" }).getByText("Service").waitFor();
    assert.equal(await page.getByRole("switch", { name: "Public access" }).isChecked(), false);
    assert.equal(await page.getByText("Everyone signed in").count(), 0);
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-access.png"), fullPage: true });

    // ---- stale hub responses --------------------------------------------------
    step("Switching agents never shows the previous agent's people while the new list loads");
    state.delays["/access"] = 700;
    await page.evaluate(() => {
      location.hash = "/agents/support/access";
    });
    await page.getByRole("heading", { name: "Who can use Support Assistant" }).waitFor();
    assert.equal(await page.getByText("Priya Natarajan").count(), 0, "finance rows must vanish immediately");
    await page.getByRole("row").filter({ hasText: "Mei Lin Chen" }).getByText("Work tickets").waitFor();
    await page.getByRole("row").filter({ hasText: "Everyone signed in" }).getByText("Public", { exact: true }).waitFor();
    await page.getByRole("row").filter({ hasText: "Mei Lin Chen" }).getByText("Awaiting approval").waitFor();
    assert.equal(await page.getByRole("switch", { name: "Public access" }).isChecked(), true);
    delete state.delays["/access"];

    // ---- staged edit: cancel writes nothing ------------------------------------
    step("Editing then cancelling writes nothing and asks before discarding");
    await go("/agents/finance/access");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
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
    step("Review lists every change and the exact requests; Save sends them in order");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    const entry = drawer().getByRole("checkbox", { name: /Use this agent/ });
    assert.equal(await entry.isDisabled(), true, "entry is locked while other capabilities are selected");
    await drawer().getByText("Required by the other selected capabilities").waitFor();
    await drawer().getByRole("checkbox", { name: /Manage invoices/ }).uncheck();
    await drawer().getByRole("checkbox", { name: /Run payroll/ }).check();
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByText("Review changes").first().waitFor();
    await drawer().getByText("This grants a sensitive capability").waitFor();
    await drawer().getByText("Each change is saved with its own request").waitFor();
    const ops = drawer().locator(".operation-list li");
    assert.equal(await ops.count(), 2);
    await ops.nth(0).getByText("Remove Manage invoices").waitFor();
    await ops.nth(1).getByText("Allow Run payroll").waitFor();
    assert.equal(state.mutations.length, 0, "review must not write");
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-review.png"), fullPage: true });
    await drawer().getByRole("button", { name: "Save 2 changes" }).click();
    await page.getByText(`Access updated for ${PRIYA}.`).waitFor();
    assert.deepEqual(state.mutations, [
      { endpoint: "/access/revoke", subject: PRIYA, hub: "finance", permission: "invoices" },
      { endpoint: "/access/grant", subject: PRIYA, hub: "finance", permission: "payroll" },
    ]);
    await priyaRow.getByText("Run payroll", { exact: true }).waitFor();
    assert.equal(await priyaRow.getByText("Manage invoices", { exact: true }).count(), 0);
    state.mutations.length = 0;

    // ---- remove all access: single cascading request ---------------------------------
    step("Remove all access sends one cascading request and explains it");
    await page.getByRole("button", { name: "Edit access for daniel.okafor@example.org" }).click();
    await drawer().getByRole("button", { name: "Remove all access" }).click();
    await drawer().getByText("will no longer be able to open Finance Assistant").waitFor();
    assert.equal(await drawer().locator(".operation-list li").count(), 1);
    await drawer().getByRole("button", { name: "Save change" }).click();
    await page.getByText("daniel.okafor@example.org no longer has direct access").waitFor();
    assert.deepEqual(state.mutations, [
      { endpoint: "/access/revoke", subject: "daniel.okafor@example.org", hub: "finance", permission: "use_hub" },
    ]);
    assert.equal(await page.getByRole("row").filter({ hasText: "daniel.okafor" }).count(), 0);
    state.mutations.length = 0;
    await page.getByRole("button", { name: "Edit access for workflow:monthly_close" }).click();
    await drawer().getByRole("button", { name: "Remove all access" }).click();
    await drawer().getByText("removes every capability in Finance Assistant").waitFor();
    await drawer().locator(".review-list").getByText("Read ledger").waitFor();
    await drawer().getByText("Remove Use this agent (and everything with it)").waitFor();
    assert.equal(await drawer().locator(".operation-list li").count(), 1, "cascade is one request, not one per capability");
    await drawer().getByRole("button", { name: "Back" }).click();
    assert.equal(await drawer().getByRole("checkbox", { name: /Read ledger/ }).isChecked(), false);
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await answer("Discard");
    await drawer().waitFor({ state: "hidden" });
    assert.equal(state.mutations.length, 0);

    // ---- add a person: validation, implied entry, minimal requests ----------------------
    step("Adding a person validates the identity and skips the implied entry grant");
    await page.getByRole("button", { name: "Add person" }).click();
    const subject = drawer().getByRole("textbox", { name: "Email address or service identity" });
    await subject.fill("not an email");
    await drawer().getByRole("checkbox", { name: /Read ledger/ }).check();
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByText("Enter a valid email address").waitFor();
    assert.equal(state.mutations.length, 0);
    await subject.fill("Ravi.Menon@example.org");
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByText("included automatically with any capability").waitFor();
    assert.equal(await drawer().locator(".operation-list li").count(), 1, "use_hub is implied by the ledger grant");
    await drawer().getByRole("button", { name: "Save change" }).click();
    await page.getByText("Access updated for ravi.menon@example.org.").waitFor();
    assert.deepEqual(state.mutations, [
      { endpoint: "/access/grant", subject: "ravi.menon@example.org", hub: "finance", permission: "ledger" },
    ]);
    await page.getByRole("row").filter({ hasText: "ravi.menon" }).getByText("Not signed up yet").waitFor();
    state.mutations.length = 0;

    step("Adding someone who already has access switches to editing their current access");
    await page.getByRole("button", { name: "Add person" }).click();
    await drawer().getByRole("textbox", { name: "Email address or service identity" }).fill(PRIYA);
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByText("already has access to Finance Assistant").waitFor();
    assert.equal(await drawer().getByRole("checkbox", { name: /Run payroll/ }).isChecked(), true);
    assert.equal(state.mutations.length, 0);
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await drawer().waitFor({ state: "hidden" });

    step("Granting to a blocked person surfaces the server's refusal with context");
    await page.getByRole("button", { name: "Add person" }).click();
    await drawer().getByRole("textbox", { name: "Email address or service identity" }).fill("tomas.herrera@example.org");
    await drawer().getByRole("button", { name: "Review changes" }).click();
    await drawer().getByRole("button", { name: "Save change" }).click();
    await drawer().getByText("Some changes were not saved").waitFor();
    await drawer().getByText("Reactivate this user before granting access").waitFor();
    await drawer().getByText("Saved 0 of 1 change").waitFor();
    await drawer().getByRole("button", { name: /Done/ }).click();
    state.mutations.length = 0;

    // ---- partial failure -----------------------------------------------------------
    step("A failing request mid-save reports what saved, what failed, and reloads current access");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    await drawer().getByRole("checkbox", { name: /Manage invoices/ }).check();
    await drawer().getByRole("checkbox", { name: /Read ledger/ }).uncheck();
    await drawer().getByRole("button", { name: "Review changes" }).click();
    state.failNext = {
      match: (endpoint, body) => endpoint === "/access/grant" && body.permission === "invoices",
      status: 503,
      detail: "Access store temporarily unavailable",
    };
    await drawer().getByRole("button", { name: "Save 2 changes" }).click();
    await drawer().getByText("Saved 1 of 2 changes").waitFor();
    await drawer().getByText("Access store temporarily unavailable").waitFor();
    await drawer().locator(".operation-list li[data-status=done]").getByText("Saved").waitFor();
    await drawer().locator(".operation-list li[data-status=failed]").getByText("Failed").waitFor();
    assert.equal(state.mutations.length, 2);
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-partial-failure.png"), fullPage: true });
    await drawer().getByRole("button", { name: /Done/ }).click();
    await drawer().waitFor({ state: "hidden" });
    assert.equal(await priyaRow.getByText("Read ledger", { exact: true }).count(), 0, "list reflects the revoke that did save");
    assert.equal(await priyaRow.getByText("Manage invoices", { exact: true }).count(), 0, "the failed grant is not shown as saved");
    state.mutations.length = 0;

    // ---- navigation guards --------------------------------------------------------------
    step("Leaving with a dirty draft (back button, typed URL) asks first; keeping editing restores the URL");
    await go("/people");
    await page.getByRole("heading", { name: "People and services" }).waitFor();
    await go("/agents/finance/access");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    await drawer().getByRole("checkbox", { name: /Read ledger/ }).check();
    await page.goBack();
    await modalTitle("Leave without saving?").waitFor();
    await answer("Keep editing");
    assert.equal(await hash(), "#/agents/finance/access");
    assert.equal(await drawer().getByRole("checkbox", { name: /Read ledger/ }).isChecked(), true);
    // The drawer mask covers the sidebar, so leaving means back/forward or a typed URL.
    await page.evaluate(() => {
      location.hash = "/people";
    });
    await answer("Discard and leave");
    await page.getByRole("heading", { name: "People and services" }).waitFor();
    assert.equal(await hash(), "#/people");
    assert.equal(state.mutations.length, 0);

    step("Navigation is refused while a save is in flight");
    await go("/agents/finance/access");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    await drawer().getByRole("checkbox", { name: /Read ledger/ }).check();
    await drawer().getByRole("button", { name: "Review changes" }).click();
    state.delays["/access/grant"] = 900;
    await drawer().getByRole("button", { name: "Save change" }).click();
    await drawer().getByRole("button", { name: /Saving 1 of 1/ }).waitFor();
    await page.evaluate(() => {
      location.hash = "/people";
    });
    await page.getByText("Wait for the current save to finish").waitFor();
    assert.equal(await hash(), "#/agents/finance/access");
    await page.getByText(`Access updated for ${PRIYA}.`).waitFor();
    delete state.delays["/access/grant"];
    state.mutations.length = 0;

    // ---- public access -------------------------------------------------------------------
    step("Public access is a reviewed switch: confirm first, one request, then reflected");
    await page.getByRole("switch", { name: "Public access" }).click();
    await modalTitle("Open Finance Assistant to everyone who signs in?").waitFor();
    assert.equal(state.mutations.length, 0);
    await answer("Open to everyone");
    await page.getByText("Finance Assistant is now open to everyone signed in.").waitFor();
    assert.deepEqual(lastMutation(), { endpoint: "/access/grant", subject: "*", hub: "finance", permission: "use_hub" });
    assert.equal(await page.getByRole("switch", { name: "Public access" }).isChecked(), true);
    await page.getByRole("row").filter({ hasText: "Everyone signed in" }).waitFor();
    state.mutations.length = 0;

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
    await page.getByRole("checkbox", { name: "Denied only" }).check();
    assert.equal(await page.getByRole("row").filter({ hasText: "Priya Natarajan" }).count(), 0);
    await page.getByText("Someone not signed in").waitFor();
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
      ["workflow:monthly_close", "Service"],
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
    await page.getByRole("heading", { name: "Agents", level: 2 }).waitFor();
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
    await page.getByRole("heading", { name: "Who can use Finance Assistant" }).waitFor();
    assert.equal(await page.getByRole("switch", { name: "Public access" }).isDisabled(), true);
    await page.getByRole("button", { name: "Edit access for Aisha Rahman" }).click();
    assert.equal(await drawer().getByRole("checkbox", { name: /Manage access/ }).isDisabled(), true);
    assert.equal(await drawer().getByRole("checkbox", { name: /Use this agent/ }).isDisabled(), true);
    await drawer().getByText("Held through organization administrator rights").waitFor();
    assert.equal(await drawer().getByRole("button", { name: "Remove all access" }).count(), 0);
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await page.getByRole("button", { name: "Edit access for Finance Admin" }).click();
    await drawer().getByText("Only organization administrators can change this.").first().waitFor();
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await go("/agents/support/access");
    await page.getByText("Agent not found").waitFor();
    await go(`/people/${encodeURIComponent(PRIYA)}`);
    await drawer().getByText("Access by agent").waitFor();
    assert.equal(await drawer().getByRole("button", { name: "Block access" }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "Refresh accounts" }).count(), 0);
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-agent-admin.png"), fullPage: true });
    state.mutations.length = 0;

    // ---- mobile -------------------------------------------------------------------------------
    step("Mobile layout keeps navigation reachable and the editor usable");
    state.role = "org";
    await page.reload(); // back to a full org-admin session
    await page.setViewportSize({ width: 390, height: 844 });
    await go("/agents/finance/access");
    await page.getByRole("button", { name: "Open navigation" }).click();
    await page.getByRole("dialog").getByRole("link", { name: "People" }).waitFor();
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-mobile-nav.png") });
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Edit access for Priya Natarajan" }).click();
    await drawer().getByRole("checkbox", { name: /Read ledger/ }).waitFor();
    await page.screenshot({ path: path.join(shots, "hubzoid-portal-mobile-editor.png") });
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await page.setViewportSize({ width: 1440, height: 950 });

    // ---- ordinary user -----------------------------------------------------------------------
    step("An ordinary user is turned away without seeing any administration UI");
    state.role = "user";
    await page.reload(); // fresh session as an ordinary (non-admin) user
    await go("");
    await page.getByText("Sign in with an account allowed to manage agent access.").waitFor();
    assert.equal(await page.getByRole("link", { name: "Agents" }).count(), 0);
    await page.getByRole("link", { name: "Go to the chat app" }).waitFor();

    assert.deepEqual(errors, [], "no console or page errors");
    console.log(`\nPASS: ${steps.length} journeys`);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(`\nFAIL at: ${steps[steps.length - 1]}\n`, error);
  process.exit(1);
});
