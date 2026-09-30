// Browser checks for the Connectors screen (personal connections registry).
// Like journey.cjs: the built assets (hubzoid/portal_dist) run against a small
// synthetic API through request interception. No server, no live data.
//
//   npm run build && node tests/connectors.cjs
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../../hubzoid/portal_dist");
const ORIGIN = "http://hubzoid.test";
const SECRET = "shown-once-never-again";

function fixture() {
  const state = {
    org: true,
    calls: [],
    connectors: [
      {
        id: "gmail", name: "Gmail", url: "https://gmail-mcp.example.org/mcp", auth_type: "oauth",
        client_id: null, has_client_secret: false, scopes: null, tool_allowlist: ["search_threads", "get_thread"],
        enabled: true, capability: "connector_gmail", dynamic_client: true, created_by: "ada@example.org",
        created_at: 1790000000, updated_at: 1790000000,
        redirect_uri: `${ORIGIN}/oauth/connectors/gmail/callback`, connections: 3,
      },
      {
        id: "linear", name: "Linear", url: "https://mcp.linear.example/sse", auth_type: "oauth",
        client_id: "hubzoid-linear", has_client_secret: true, scopes: "read write", tool_allowlist: null,
        enabled: true, capability: "connector_linear", dynamic_client: false, created_by: "ada@example.org",
        created_at: 1790000000, updated_at: 1790000000,
        redirect_uri: `${ORIGIN}/oauth/connectors/linear/callback`, connections: 0,
      },
    ],
  };
  const fail = (status, code, message) => Object.assign(new Error(message), { status, code, message });
  function handle(method, endpoint, body) {
    if (endpoint === "/me")
      return { subject: "ada@example.org", org_admin: state.org, manageable: ["sales"], grantable: { sales: [] },
               account_admin: state.org, can_create_accounts: false, accounts_configured: true,
               sign_in: { password: true, google: false } };
    if (endpoint === "/hubs") return { hubs: [{ key: "sales", name: "Sales Assistant", authoritative: true }] };
    const m = endpoint.match(/^\/connectors(?:\/([^/]+))?(\/test)?$/);
    if (!m) throw fail(404, "not_found", "Unknown endpoint");
    if (!state.org) throw fail(403, "forbidden", "Only organization administrators manage connectors.");
    state.calls.push({ method, endpoint, body });
    const [, id, test] = m;
    const found = id && state.connectors.find((c) => c.id === id);
    if (!id && method === "GET") return { connectors: state.connectors };
    if (!id && method === "POST") {
      if (state.connectors.some((c) => c.id === body.id)) throw fail(409, "exists", `A connector with the ID ${body.id} already exists.`);
      const c = {
        id: body.id, name: body.name, url: body.url, auth_type: body.auth_type, client_id: body.client_id ?? null,
        has_client_secret: !!body.client_secret, scopes: body.scopes ?? null, tool_allowlist: body.tool_allowlist ?? null,
        enabled: body.enabled, capability: `connector_${body.id}`, dynamic_client: false, created_by: "ada@example.org",
        created_at: 1790000100, updated_at: 1790000100, redirect_uri: `${ORIGIN}/oauth/connectors/${body.id}/callback`,
        connections: 0,
      };
      state.connectors.push(c);
      return { connector: c };
    }
    if (!found) throw fail(404, "not_found", "No connector has this ID.");
    if (test)
      return found.id === "linear"
        ? { connector_id: found.id, auth_type: "oauth", ok: false, redirect_uri: found.redirect_uri,
            error: { code: "unreachable", message: "The server could not be reached. Check the URL and that the server is running." } }
        : { connector_id: found.id, auth_type: "oauth", ok: true, redirect_uri: found.redirect_uri, requires_auth: true,
            resource: found.url, issuer: "https://auth.example.org", authorization_endpoint: "https://auth.example.org/authorize",
            token_endpoint: "https://auth.example.org/token", registration_endpoint: "https://auth.example.org/register",
            revocation_endpoint: "https://auth.example.org/revoke", iss_parameter_supported: true, pkce: "S256",
            scopes_supported: ["mail.read"], default_scope: "mail.read", scope: "mail.read", registration: "dynamic",
            token_endpoint_auth_methods: ["none"], notes: [] };
    if (method === "PATCH") {
      const { client_secret, ...rest } = body;
      Object.assign(found, rest);
      if (client_secret !== undefined) found.has_client_secret = !!client_secret;
      return { connector: found };
    }
    if (method === "DELETE") {
      state.connectors = state.connectors.filter((c) => c !== found);
      return undefined;
    }
    throw fail(405, "method", "Not allowed");
  }
  return { state, handle };
}

const steps = [];
const step = (name) => {
  steps.push(name);
  console.log(`· ${name}`);
};

(async () => {
  const browser = await chromium.launch({ headless: true });
  const fx = fixture();
  let page;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
    page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("console", (m) => {
      if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text());
    });
    await context.route(`${ORIGIN}/**`, async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.startsWith("/portal/api")) {
        const endpoint = url.pathname.slice("/portal/api".length);
        const body = ["POST", "PATCH"].includes(request.method()) && request.postData() ? request.postDataJSON() : undefined;
        try {
          const data = fx.handle(request.method(), endpoint, body);
          return data === undefined ? route.fulfill({ status: 204, body: "" }) : route.fulfill({ json: data });
        } catch (e) {
          return route.fulfill({ status: e.status || 500, json: { detail: { code: e.code, message: e.message } } });
        }
      }
      const relative = url.pathname.replace(/^\/portal\/?/, "") || "index.html";
      const file = path.join(root, relative);
      const exists = fs.existsSync(file) && fs.statSync(file).isFile();
      return route.fulfill({
        body: fs.readFileSync(exists ? file : path.join(root, "index.html")),
        contentType: file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html",
      });
    });
    const drawer = () => page.locator(".ant-drawer-section[role=dialog]");
    // The list reloads after each change, so look for the last write.
    const last = () => [...fx.state.calls].reverse().find((c) => c.method !== "GET");

    step("Organization administrators find Connectors in the navigation");
    await page.goto(`${ORIGIN}/portal/#/agents`);
    await page.getByRole("menuitem", { name: "Connectors" }).click();
    await page.getByRole("heading", { name: "Connectors", level: 1 }).waitFor();
    assert.equal(await page.evaluate(() => location.hash), "#/connectors");

    step("The list shows each connector's sign-in, tools, people and switch");
    const gmail = page.getByRole("row").filter({ hasText: "Gmail" });
    await gmail.getByText("gmail · https://gmail-mcp.example.org/mcp").waitFor();
    await gmail.getByText("2 allowed").waitFor();
    await gmail.getByText("3 people").waitFor();
    const linear = page.getByRole("row").filter({ hasText: "Linear" });
    await linear.getByText("OAuth · own client").waitFor();
    await linear.getByText("All tools").waitFor();
    assert.equal(await linear.getByRole("switch").isChecked(), true);

    step("Adding checks the fields before sending anything");
    await page.getByRole("button", { name: "Add connector" }).first().click();
    await drawer().getByRole("button", { name: "Add connector" }).click();
    await drawer().getByText("Enter a name people will recognise, for example Gmail.").waitFor();
    await drawer().getByText("Enter the server’s URL.").waitFor();
    assert.equal(fx.state.calls.filter((c) => c.method === "POST").length, 0);
    await drawer().getByLabel("Name").fill("Google Drive");
    assert.equal(await drawer().getByLabel("ID", { exact: true }).getAttribute("placeholder"), "google_drive");
    await drawer().getByText("Also the capability connector_google_drive. It can’t be changed later.").waitFor();
    await drawer().getByLabel("Server URL").fill("http://drive.example.org/mcp");
    await drawer().getByText("Use https:// (plain http only on this computer, for development).").waitFor();
    await drawer().getByLabel("Server URL").fill("https://drive.example.org/mcp");
    await drawer().getByText(`${ORIGIN}/oauth/connectors/google_drive/callback`).waitFor();
    await drawer().getByLabel("Client ID (optional)").fill("drive-client");
    await drawer().getByLabel("Client secret (optional)").fill(SECRET);

    step("A new connector is saved with its secret once, then tested at once");
    await drawer().getByRole("button", { name: "Add connector" }).click();
    await page.getByText("Google Drive was added.").waitFor();
    const created = fx.state.calls.find((c) => c.method === "POST" && c.endpoint === "/connectors");
    assert.deepEqual(created.body, {
      name: "Google Drive", url: "https://drive.example.org/mcp", auth_type: "oauth", scopes: null,
      tool_allowlist: null, enabled: true, client_id: "drive-client", client_secret: SECRET, id: "google_drive",
    });
    await page.getByRole("dialog", { name: "Test Google Drive" }).getByText("Discovery succeeded").waitFor();
    await drawer().getByText("Hubzoid registers itself (dynamic registration)").waitFor();
    assert.ok(fx.state.calls.some((c) => c.method === "POST" && c.endpoint === "/connectors/google_drive/test"));
    await drawer().getByRole("button", { name: "Close" }).click();
    await drawer().waitFor({ state: "hidden" });
    assert.equal(await page.getByText(SECRET).count(), 0, "a secret is never shown again");

    step("A refused save keeps the drawer open with the server's reason");
    await page.getByRole("button", { name: "Add connector" }).first().click();
    await drawer().getByLabel("Name").fill("Gmail");
    await drawer().getByLabel("Server URL").fill("https://other.example.org/mcp");
    await drawer().getByRole("button", { name: "Add connector" }).click();
    await drawer().getByText("A connector with the ID gmail already exists.").waitFor();
    await drawer().getByRole("button", { name: "Cancel" }).click();
    await page.getByRole("button", { name: "Discard" }).click();
    await drawer().waitFor({ state: "hidden" });

    step("Editing keeps a stored secret unless it is replaced or removed");
    await page.getByRole("link", { name: "Linear" }).click();
    await drawer().getByText("A secret is stored. Leave empty to keep it. It is never shown again.").waitFor();
    assert.equal(await drawer().getByLabel("ID", { exact: true }).isDisabled(), true);
    await drawer().getByLabel("Scopes (optional)").fill("read");
    await drawer().getByRole("button", { name: "Save" }).click();
    await page.getByText("Linear was saved.").waitFor();
    const edit = last();
    assert.equal(edit.method, "PATCH");
    assert.equal(edit.endpoint, "/connectors/linear");
    assert.equal(edit.body.scopes, "read");
    assert.equal("client_secret" in edit.body, false, "an untouched secret is not sent");
    assert.equal("id" in edit.body, false);

    step("A test that fails says why");
    await linear.getByRole("button", { name: "Test" }).click();
    await drawer().getByText("Not ready").waitFor();
    await drawer().getByText("The server could not be reached. Check the URL and that the server is running.").waitFor();
    await drawer().getByRole("button", { name: "Close" }).click();

    step("The switch turns a connector off");
    await linear.getByRole("switch").click();
    await page.getByText("Linear is switched off.").waitFor();
    assert.deepEqual(last().body, { enabled: false });

    step("Removing asks first and says what happens to connected people");
    await gmail.getByRole("button", { name: "More actions for Gmail" }).click();
    await page.getByRole("menuitem", { name: "Remove" }).click();
    await page.getByText("3 people lose their connections.", { exact: false }).waitFor();
    await page.getByRole("button", { name: "Remove connector" }).click();
    await page.getByText("Gmail was removed.").waitFor();
    assert.equal(last().method, "DELETE");
    assert.equal(await page.getByRole("row").filter({ hasText: "gmail-mcp" }).count(), 0);

    step("At phone width the page does not scroll sideways");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("heading", { name: "Connectors", level: 1 }).waitFor();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.setViewportSize({ width: 1440, height: 950 });

    step("Agent administrators see neither the entry nor the page");
    fx.state.org = false;
    await page.goto(`${ORIGIN}/portal/?again#/connectors`);
    await page.getByText("Page not found").waitFor();
    assert.equal(await page.getByRole("menuitem", { name: "Connectors" }).count(), 0);

    assert.deepEqual(errors, [], "no page errors");
    console.log(`\nPASS: ${steps.length} connector checks`);
  } catch (e) {
    console.error(`\nFAIL at: ${steps[steps.length - 1]}\n`, e);
    if (page) {
      const shots = process.env.PORTAL_SHOTS || path.join(require("node:os").tmpdir(), "hubzoid-console-tests");
      fs.mkdirSync(shots, { recursive: true });
      await page.screenshot({ path: path.join(shots, "connectors-failure.png"), fullPage: true }).catch(() => {});
    }
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
