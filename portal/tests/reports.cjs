// Focused personal-workspace journey. Synthetic data, one browser, no hub.
// Run only after a frontend build: npm run test:reports.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { settingsHost } = require("./settings-host.cjs");
const root = path.resolve(__dirname, "../../hubzoid/portal_dist");

(async () => {
  const browser = await chromium.launch({ headless: true });
  // A person who uses agents but doesn't manage the deployment.
  const host = await settingsHost({ as: "sam@example.com" });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    let reads = 0, saved;
    const schema = { type: "object", properties: { service: { type: "string" } }, required: ["service"] };
    const schedule = {
      id: "psdemo", hub: "ops", name: "Weekly service snapshot", workflow: "service_snapshot", report: "Service snapshot",
      inputs: "checkout", when: "Mondays at 09:00", timezone: "Asia/Kolkata", state: "active", state_label: "Active",
      reason: null, next_fire: 1792371000, last_status: "running", last_label: "Running now", last_at: null,
      last_run: "ps-psdemo-first", reports: [], recovery: null,
    };
    const my = {
      subject: "person@example.org", admin: false, blocked: false, web_app: true, connections: false,
      agents: [{ key: "ops", name: "Operations", api: "/portal/api", chat: "/?agent=ops" }],
      schedules: [schedule], reports: [],
      catalog: [{ hub: "ops", workflow: "service_snapshot", title: "Service snapshot", schedulable: true, schema }],
    };
    await page.route("http://hubzoid.test/**", async (route) => {
      const request = route.request(), url = new URL(request.url());
      if (url.pathname.startsWith("/portal/api")) {
        const ep = url.pathname.slice("/portal/api".length);
        if (ep === "/my") { reads++; return route.fulfill({ json: my }); }
        if (ep === "/my/runs") return route.fulfill({ json: { runs: [{
          id: "tool-service_snapshot-person-input-12345678", report: "Service snapshot", status: "SUCCESS",
          started: Date.now(), links: [{ title: "My completed report", url: "/portal/a/own" }],
          answer: "The requested service is healthy.", recovery: null,
        }] } });
        if (ep === "/my/schedules/psdemo" && request.method() === "GET") return route.fulfill({ json: {
          inputs: { service: "checkout" }, cadence: "every monday at 09:00", timezone: "Asia/Kolkata", revision: 123, schema,
        } });
        if (ep === "/my/schedules/psdemo" && request.method() === "PUT") {
          saved = request.postDataJSON(); return route.fulfill({ json: { ok: true } });
        }
        if (ep === "/my/schedules/psdemo/runs") return route.fulfill({ json: { runs: [{
          id: "ps-psdemo-first", status: "PENDING", started: Date.now(), completed: null, duration_ms: null,
        }] } });
        if (ep.startsWith("/my/schedules/psdemo/")) return route.fulfill({ json: { ok: true } });
        return route.fulfill({ status: 403, json: { detail: "Admin only" } });
      }
      if (await host.forward(route, url)) return;
      const relative = url.pathname.replace(/^\/(portal\/?)?/, "") || "index.html";
      const file = path.join(root, relative);
      return route.fulfill({ body: fs.readFileSync(fs.existsSync(file) && fs.statSync(file).isFile() ? file : path.join(root, "index.html")),
        contentType: file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html" });
    });
    await page.clock.install();
    // The old Console address opens the person's schedules in Settings.
    await page.goto("http://hubzoid.test/portal/#/schedules");
    await page.waitForURL(/\/settings#\/me\/schedules$/);
    // One page: the repeating requests, then the reports (HUB-140 settings plan).
    await page.getByRole("heading", { name: "Repeating requests" }).waitFor();
    await page.getByRole("heading", { name: "Reports you can run" }).waitFor();
    await page.getByRole("link", { name: "My completed report" }).waitFor();
    const scheduleButton = page.getByRole("button", { name: "Weekly service snapshot", exact: true });
    await scheduleButton.focus();
    await page.keyboard.press("Enter");
    const drawer = page.getByRole("dialog");
    await drawer.getByText("Latest runs", { exact: true }).waitFor();
    await drawer.getByRole("button", { name: "Edit", exact: true }).click();
    const editor = page.getByRole("dialog", { name: "Edit Weekly service snapshot" });
    await editor.getByLabel("Service", { exact: true }).fill("search");
    await editor.getByLabel("When", { exact: true }).fill("daily at 18:00");
    await editor.getByRole("button", { name: "Save changes" }).click();
    await editor.waitFor({ state: "hidden" });
    assert.deepEqual(saved.inputs, { service: "search" });
    assert.equal(saved.revision, 123);
    assert.equal(saved.cadence, "daily at 18:00");
    await page.getByRole("dialog").getByRole("button", { name: "Close", exact: true }).click();
    const before = reads;
    schedule.last_status = "updated"; schedule.last_label = "Updated just now";
    await page.clock.fastForward(11000);
    await page.getByText("Updated just now", { exact: true }).waitFor();
    assert.ok(reads > before, "Visible schedules must refresh");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("table").waitFor({ state: "hidden" });
    const pause = page.getByRole("button", { name: "Pause", exact: true });
    await pause.scrollIntoViewIfNeeded();
    const box = await pause.boundingBox();
    assert.ok(box && box.x >= 0 && box.x + box.width <= 390, "Pause must be reachable without horizontal scrolling");
    await page.getByRole("button", { name: "Weekly service snapshot", exact: true }).focus();
    await page.keyboard.press("Enter");
    await page.getByRole("dialog").getByText("Latest runs", { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    console.log("Personal reports: discovery, results, keyboard, editing, refresh and mobile controls passed.");
  } finally { await browser.close(); await host.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
