// Browser regression checks use synthetic accounts; no customer service is contacted.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "../../hubzoid/portal_dist");

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 950 },
    });
    let role = "org",
      delayOps = false,
      rejectMutation = false;
    const mutations = [];
    const perms = (hub) =>
      [
        "use_hub",
        "manage_access",
        hub === "finance" ? "ledger" : "inventory",
      ].map((permission) => ({
        permission,
        label: permission,
        description: "",
        sensitive: false,
      }));
    await page.route("http://hubzoid.test/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith("/portal/api")) {
        const endpoint = url.pathname.slice("/portal/api".length);
        if (role === "user")
          return route.fulfill({
            status: 403,
            json: { detail: "Not an administrator" },
          });
        let data;
        if (endpoint === "/me")
          data = {
            subject: "admin@example.com",
            org_admin: role === "org",
            manageable: ["finance", "ops"],
          };
        if (endpoint === "/hubs")
          data = {
            hubs: [
              { key: "finance", name: "Finance", authoritative: true },
              { key: "ops", name: "Operations", authoritative: true },
            ],
          };
        if (endpoint === "/overview")
          data = {
            hubs: 2,
            people: 12,
            grants: 40,
            managed: 2,
            legacy: 0,
            visibility: { state: "ok" },
          };
        if (endpoint === "/access") {
          const hub = url.searchParams.get("hub");
          if (hub === "ops" && delayOps)
            await new Promise((resolve) => setTimeout(resolve, 700));
          data = {
            hub,
            authoritative: true,
            can_manage_admins: role === "org",
            public: false,
            total: 1,
            permissions: perms(hub),
            rows: [
              {
                subject:
                  hub === "finance" ? "alice@example.com" : "bob@example.com",
                display: hub === "finance" ? "Alice" : "Bob",
                status: "active",
                kind: "person",
                perms: ["use_hub"],
                inherited: [],
                effective: ["use_hub"],
              },
            ],
          };
        }
        if (endpoint === "/access/grant" || endpoint === "/access/revoke") {
          mutations.push(route.request().postDataJSON());
          if (rejectMutation)
            return route.fulfill({
              status: 409,
              json: { detail: "Please retry this change" },
            });
          data = { ok: true };
        }
        if (endpoint === "/workflows")
          data = {
            workflows: [
              {
                hub: url.searchParams.get("hub"),
                name: "daily_report",
                schedule: "daily 06:00",
                timezone: "UTC",
                state: "scheduled",
                next_run: null,
                missed: 0,
              },
            ],
          };
        if (endpoint === "/runs")
          data = {
            runs: [
              {
                hub: "ops",
                id: "run-123",
                name: "daily_report",
                status: "SUCCESS",
                started: 1700000000000,
                completed: 1700000001000,
                duration_ms: 1000,
                output: "Completed 12 records",
                error: null,
                steps: [{ name: "fetch_records", output: "12", error: null }],
              },
            ],
          };
        if (endpoint === "/access-changes")
          data = {
            rows: [
              {
                hub: "ops",
                actor: "admin@example.com",
                subject: "bob@example.com",
                action: "grant",
                permission: "inventory",
                ts: 1700000000,
              },
            ],
          };
        if (endpoint === "/audit") data = { rows: [] };
        if (endpoint === "/people") data = { people: [] };
        return route.fulfill({ json: data || {} });
      }
      const relative = url.pathname.replace("/portal/", "") || "index.html";
      const file = path.join(root, relative);
      return route.fulfill({
        body: fs.readFileSync(file),
        contentType: file.endsWith(".js")
          ? "application/javascript"
          : file.endsWith(".css")
            ? "text/css"
            : "text/html",
      });
    });
    await page.goto("http://hubzoid.test/portal/");
    await page.getByRole("button", { name: "Access", exact: true }).click();
    await page.getByText("Alice", { exact: true }).waitFor();
    delayOps = true;
    await page
      .getByRole("combobox", { name: "Agent", exact: true })
      .selectOption("ops");
    assert.equal(
      await page.getByRole("button", { name: "ledger", exact: true }).count(),
      0,
      "stale permissions must disappear immediately",
    );
    await page.getByText("Bob", { exact: true }).waitFor();
    await page.getByRole("button", { name: "inventory", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "Saved" }).waitFor();
    assert.deepEqual(mutations[0], {
      subject: "bob@example.com",
      hub: "ops",
      permission: "inventory",
    });
    await page
      .getByRole("button", { name: "inventory", exact: true })
      .waitFor();
    rejectMutation = true;
    await page.getByRole("button", { name: "inventory", exact: true }).click();
    await page.getByRole("alert").waitFor();
    assert.equal(
      await page
        .getByRole("button", { name: "inventory", exact: true })
        .isVisible(),
      true,
      "mutation errors must not replace the panel",
    );
    rejectMutation = false;
    await page.getByRole("button", { name: "Workflows", exact: true }).click();
    await page.getByRole("button", { name: "View runs" }).click();
    await page.getByRole("button", { name: "run-123" }).click();
    await page.getByText("Completed 12 records").waitFor();
    await page.getByRole("button", { name: "Audit", exact: true }).click();
    await page.getByText("grant inventory").waitFor();
    role = "hub";
    await page.reload();
    await page.getByRole("button", { name: "Access", exact: true }).click();
    await page
      .getByRole("button", { name: "manage_access", exact: true })
      .waitFor();
    assert.equal(
      await page
        .getByRole("button", { name: "manage_access", exact: true })
        .isDisabled(),
      true,
    );
    const selectedStyle = await page
      .getByRole("button", { name: "use_hub", exact: true })
      .evaluate((el) => {
        const s = getComputedStyle(el);
        return { color: s.color, background: s.backgroundColor };
      });
    assert.notEqual(
      selectedStyle.color,
      selectedStyle.background,
      "selected permission must have visible text",
    );
    await page.screenshot({
      path: "/private/tmp/hubzoid-access-fixed.png",
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
      path: "/private/tmp/hubzoid-access-mobile.png",
      fullPage: true,
    });
    role = "user";
    await page.reload();
    await page.getByText("Not an administrator").waitFor();
    assert.equal(
      await page.getByRole("button", { name: "Access", exact: true }).count(),
      0,
    );
    console.log(
      "PASS: hub-switch isolation, correct mutation target, recoverable errors, run details, access history, admin controls, ordinary-user denial",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
