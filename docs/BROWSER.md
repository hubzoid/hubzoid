# Shared browser (Playwright)

Give every agent in a hub a **web browser** — one shared, resource-limited
browser exposed as the full Playwright toolset (`browser_navigate`,
`browser_click`, `browser_snapshot`, …).

The point: **N agents no longer mean N browsers.** One browser is shared across
the whole hub, and hard limits keep it from overloading the box.

---

## 🚀 Turn it on

In the hub's `.env`:

```
HUBZOID_BROWSER=true
```

Restart the hub. Every agent now has the browser tools and knows how to use
them. No `.mcp.json` edits, no code.

> One exception: an agent that **pins** an explicit `tools: [...]` list in its
> frontmatter won't see `browser_*` until you add them to that list. Agents
> with no pinned list get them automatically.

---

## 🧭 Two ways it runs

| Mode | When | What you get |
|---|---|---|
| **Direct** | nothing else set | One shared browser, spawned by HubZoid. Shares the browser; memory bounded by the host + optional RSS watchdog. Great for dev / a small hub. |
| **Pooled** | `HUBZOID_BROWSER_CDP_URL` set (browserless) | One shared browser **pool** with a hard cap: one action at a time, the rest **queue**, stuck sessions time out, memory ceiling enforced. Recommended for production. |

Direct needs only Node (`npx`). Pooled adds a browserless container.

> **Direct mode needs the Playwright browser installed on the host.** The
> sidecar uses the bundled Chromium (`--browser chromium`); install it once with
> `npx -p @playwright/mcp@latest playwright install chromium`. (Pooled mode needs
> nothing on the host — the browser lives in the browserless container.)

---

## 🏭 Production: pooled + limited (recommended)

Just include the browser compose file — **nothing to hand-set:**

```
docker compose -f docker/docker-compose.yml -f docker/browser-compose.yml up
```

That file turns the browser on for the hub and points it at the sidecar for you
(it sets `HUBZOID_BROWSER` and `HUBZOID_BROWSER_MCP_URL` on the hub container),
so you don't edit `.env` at all. `browserless` enforces the limits; `playwright-mcp`
is the MCP face agents talk to.

Why the URL isn't assumed: in compose the sidecar is a *separate container*
(`playwright-mcp`), reachable by that service name, not `localhost` — so the
compose file supplies it. In dev/direct mode HubZoid runs the sidecar on
`localhost:8931` and assumes it, so there you set nothing but the flag.

---

## ⚙️ Knobs

| Var | Default | What |
|---|---|---|
| `HUBZOID_BROWSER` | `false` | Master switch. |
| `HUBZOID_BROWSER_PORT` | `8931` | Sidecar port (direct/dev). |
| `HUBZOID_BROWSER_MCP_URL` | — | Point at an externally-run sidecar; HubZoid then spawns nothing. |
| `HUBZOID_BROWSER_CDP_URL` | — | Attach the sidecar to a browser pool for hard limits. **Must be** `ws://host:3000?token=…` (the `http://` form drops the token). |
| `HUBZOID_BROWSER_CHANNEL` | `chromium` | Browser build. Keep `chromium` in containers (no system Chrome there). |
| `HUBZOID_BROWSER_CONCURRENT` | `2` | Parallel browser slots. **This is the memory dial** — ~300–430 MB per live slot. |
| `HUBZOID_BROWSER_QUEUED` | `5` | Waiters before new requests are rejected. |
| `HUBZOID_BROWSER_TIMEOUT` | `60` | Seconds; reaps stuck sessions. |
| `HUBZOID_BROWSER_MEMORY` | `2g` | Hard container memory ceiling (the real cap). |
| `HUBZOID_BROWSER_MAX_RSS_MB` | `0` (off) | Direct-mode only: restart the browser if its RSS exceeds this. |

---

## 🧠 How memory actually behaves

- Idle / connected-but-not-browsing agents cost **~0**.
- Cost scales with **concurrent** sessions, not with how many agents are
  connected: ~430 MB for 1 live session, +~300 MB per additional one.
- So `CONCURRENT` bounds memory; the container `--memory` is the hard ceiling.

---

## ✅ Test it

```
pytest -m e2e_browser        # spawns a real sidecar; pooled case needs Docker
```

## Pooled without docker-compose (systemd or bare bridges)

Compose is one deployment option. For a hub running in a host virtualenv or a
systemd unit, run the sidecars separately and set the hub environment explicitly.
The following matches the pinned Playwright MCP v0.0.81 integration:

```sh
docker network create hubzoid-browser
docker run -d --name browserless --network hubzoid-browser \
  --restart unless-stopped --memory 2g \
  -e CONCURRENT=2 -e QUEUED=5 -e TIMEOUT=60000 -e TOKEN=hubzoid \
  ghcr.io/browserless/chromium:latest
docker run -d --name playwright-mcp --network hubzoid-browser \
  --restart unless-stopped -p 127.0.0.1:8931:8931 \
  --entrypoint node mcr.microsoft.com/playwright/mcp:v0.0.81 \
  /app/cli.js --host=0.0.0.0 --port=8931 --headless \
  '--cdp-endpoint=ws://browserless:3000?token=hubzoid'
```

Set these in the hub's `.env` or systemd environment, then restart the bridge:

```dotenv
HUBZOID_BROWSER=true
HUBZOID_BROWSER_MCP_URL=http://localhost:8931/mcp
```

Use `localhost` in the MCP URL: this pinned sidecar validates the HTTP Host
header, and `127.0.0.1` may be refused. Publishing to loopback keeps the sidecar
private. The clean `node /app/cli.js` entrypoint avoids the image's local-browser
flags competing with `--cdp-endpoint`. Do not add `--isolated` in pooled mode.
A hub-local `.mcp.json` `playwright` entry still overrides this shared browser.
For reproducible deployments, replace the browserless `latest` tag with your
validated image digest when provisioning the host.
