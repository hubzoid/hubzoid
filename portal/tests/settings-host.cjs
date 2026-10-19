// The Console's screens live in the chat app's Settings page. Browser tests
// that drive them through request interception use this host for the chat
// app's own calls (session, branding, agents, preferences): the app fixture,
// signed in as the local owner, or as `as` (a seeded account) when a test needs
// a person who doesn't manage the deployment. The Console API (/portal/api)
// stays with each test's own synthetic fixture.
const { createApp, PASSWORD } = require("./app-fixture-server.cjs");

async function settingsHost({ as } = {}) {
  const app = createApp({ mode: as ? "accounts" : "local" });
  const base = `http://127.0.0.1:${await app.listen(0)}`;
  let cookie;
  if (as) {
    const r = await fetch(`${base}/api/auth/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email: as, password: PASSWORD }),
    });
    cookie = (r.headers.get("set-cookie") || "").split(";")[0];
    if (!r.ok || !cookie) throw new Error(`fixture sign-in failed for ${as}`);
  }
  return {
    app,
    // Answer a chat-app request from the app fixture; false when it is not one.
    async forward(route, url) {
      if (!url.pathname.startsWith("/api/") && !url.pathname.startsWith("/branding/")) return false;
      const headers = { ...route.request().headers(), ...(cookie ? { cookie } : {}) };
      const response = await route.fetch({ url: base + url.pathname + url.search, headers });
      await route.fulfill({ response });
      return true;
    },
    close: () => app.close(),
  };
}

module.exports = { settingsHost };
