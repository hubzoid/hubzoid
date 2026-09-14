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
