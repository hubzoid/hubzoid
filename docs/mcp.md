# MCP connectors

[Model Context Protocol](https://modelcontextprotocol.io) servers attach as
additional tool sources to your agent. Configure them in
`<hub>/connectors/.mcp.json`.

## Format

The same shape Claude Desktop uses:

```json
{
  "mcpServers": {
    "<name>": {
      "command": "...",      // for stdio transport
      "args": ["..."],
      "env": {"VAR": "..."}
    },
    "<name2>": {
      "transport": "sse",    // for SSE transport
      "url": "https://...",
      "headers": {"Authorization": "Bearer ${TOKEN}"}
    }
  }
}
```

## Env-var interpolation

`${VAR}` references in any string field are resolved against the environment
at boot. Useful for tokens:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GH_TOKEN}"}
    }
  }
}
```

Set `GH_TOKEN` in your hub's `.env`; the platform loads it before MCP
startup.

## Useful servers

| Server | Use case |
|---|---|
| `@modelcontextprotocol/server-filesystem` | Read files under a directory |
| `@modelcontextprotocol/server-github` | Issues, PRs, code search |
| `@modelcontextprotocol/server-postgres` | Read-only SQL queries |
| `@modelcontextprotocol/server-slack` | Read channels (requires Slack token) |

See https://github.com/modelcontextprotocol/servers for the full list.

## Safety

Every MCP server is provisioned read-only by default (no writes, no posts).
Granting write access is a per-server decision. set up the server's
credentials with the right scope before adding to `.mcp.json`.

On `claude-local`, the agent sees only the MCP servers Hubzoid passes it: the
hub's own tools, the servers in its `.mcp.json`, the shared browser, and the
servers the current person connected in Open WebUI. It never loads the
connectors of the Claude account the box is signed in to (claude.ai Gmail,
Drive, Slack and so on) or MCP servers from that account's user or project
settings. `hub.call_llm` and the eval judge get no tools or servers at all.
Hubzoid refuses to start a Claude run with an SDK that cannot enforce this.

MCP credentials stay off the command line. On `claude-local`, the headers, URLs
and `env` of the hub's servers and of each person's own connections are written
to a new file readable only by the account running Hubzoid (mode 0600), one per
chat turn. That file holds only that person's connections and is deleted when the
turn ends, fails or is cancelled. Other accounts on the machine cannot see these
credentials in the process list. Limits:

- It does not protect against root, or against other processes running as the
  same account as Hubzoid. They can read the file while a turn runs, and each
  MCP server's own environment.
- If the Hubzoid process is killed outright (for example `kill -9`) during a
  turn, that turn's file stays in the system temp directory, still readable only
  by the same account, until the OS clears it.
- A secret written into a stdio server's `args` is still visible in that
  server's own process arguments. Pass secrets to stdio servers in `env`.

## Per-user MCP via Open WebUI (native OAuth)

The `.mcp.json` connectors above are hub-wide: one credential shared by every
user. For tools where each user must act as **themselves** (their own Jira,
Linear, Odoo, ...), Hubzoid instead picks up MCP servers registered in **Open
WebUI**, where each user connects their own account. **No Hubzoid UI, no
`.mcp.json`** - OWUI's own admin screen is the source of truth.

### How it works

1. An **admin registers** the MCP server once in OWUI (steps below). OWUI
   persists it to its database.
2. Each **user connects** their own account via `+ -> Integrations -> Tools`
   (an OAuth redirect). OWUI vaults that user's token, encrypted.
3. On the user's next turn the bridge **reads and decrypts their token** from
   OWUI's database and calls the MCP server as them. Two users reach the same
   server as themselves. The connection follows their identity to other
   surfaces that map to the same OWUI account, but only surfaces allowed to
   reach restricted tools (`HUBZOID_RESTRICTED_SURFACES`). A shared Slack
   channel never carries it. All three backends use it (see
   [Runtimes](#runtimes) below).

### Enable it (operator - one line)

Add to the hub's `.env`:

```dotenv
OWUI_NATIVE_MCP=true
```

That single switch turns on the bridge injection **and** configures OWUI for it
- it expands to `ENABLE_PERSISTENT_CONFIG=True` (so admin-registered servers
persist to the DB the bridge reads; OWUI keeps that config in memory otherwise)
plus the tools permissions hubzoid strips by default. It is **opt-in**, so hubs
that do not use it stay env-authoritative and reproducible.

You also need a fixed **`WEBUI_SECRET_KEY`** in the `.env`: OWUI encrypts the
tokens with it and the bridge decrypts with the same value (`hubzoid run` hands
both processes the `.env` value). Generate one with `openssl rand -hex 32`.
Requires OWUI >= 0.6.31.

**First boot** with the flag does a one-time reseed of OWUI's config from your
env (a `config`-table-only reset so env is the true default). Users, groups,
models, access grants and already-registered tool servers are untouched. After
that, env is the default and admin edits in OWUI persist normally.

**Gateway mode:** set `OWUI_NATIVE_MCP=true` at the **gateway** level - one
shared OWUI means one tool-server registry and one token store, so it is
gateway-wide: every hub bridge injects. Hubzoid does not read OWUI's own access
settings for a tool server. On a managed hub the `connector_<app>` capability is
the gate (see below). Bridges read the shared gateway DB automatically. The OAuth redirect
returns to the shared OWUI, so set `WEBUI_URL` / `HUBZOID_PUBLIC_URL` to your
real public URL or the provider redirect will fail.

### Register a server (admin - once per server, in OWUI)

1. **Admin Panel -> Settings -> Integrations**
2. **External Tool Servers -> +**
3. **Type ->** switch to **MCP Streamable HTTP**
4. Fill **URL** (e.g. `https://mcp.linear.app/mcp`), a **Name**, and
   **Auth -> OAuth 2.1**
5. **Register Client** -> wait for **"Registered"** (RFC 7591 dynamic client
   registration against the server)
6. **Save** (the dialog), then **Save** (the page)

> The server must support **Dynamic Client Registration**. Linear, Notion,
> Sentry, and Atlassian do. **GitHub's remote MCP does not** (no `/register`
> endpoint) - for servers without DCR, use **OAuth 2.1 (Static)** with a
> pre-created OAuth app instead.

### Connect (each user - once, in a chat)

1. Click **Integrations** (next to the `+`) -> **Tools** -> toggle the server on
2. Complete the provider's **OAuth** sign-in
3. Ask the agent to use it. From then on the bridge injects the user's token
   automatically.

### Notes and limits

- **Governance:** which servers exist is admin-controlled (registered globally
  in OWUI). A user only connects their own account to them.
- **Token refresh** is automatic: an expired token is refreshed via its refresh
  token and written back to OWUI's `oauth_session`, so OWUI and the bridge stay
  in sync (single source of truth). A user only reconnects if the refresh token
  itself is revoked or has expired.
- **Turn it off:** remove `OWUI_NATIVE_MCP` (or set it to `0`).

### Runtimes

Implemented. One per-turn source (`hubzoid/owui_mcp.py`, `per_user_servers`)
feeds all three backends, so a hub behaves the same whichever backend it runs.

| Backend | How the caller's servers join a turn | Tool names |
|---|---|---|
| Claude (`claude-local`) | Per-turn copy of the SDK options with an `http` MCP spec per server | `mcp__owui_<name>__<tool>` |
| OpenAI Agents | Per-turn Streamable HTTP clients and a per-turn clone of the agent | the MCP tool name |
| Codex (`codex-local`) | Per-turn copy of the tool registry. Hubzoid runs the MCP client and the Codex app-server only sees dynamic tools | the MCP tool name |

Rules that hold on all three:

- The token rides only in the MCP client's `Authorization` header. It never
  enters the prompt, a log line or a tool result.
- The admin's per-server tool allow-list applies.
- A personal tool never shadows a hub tool. On a name clash the personal
  server is skipped for that turn and a warning is logged. Claude namespaces
  every server, so there the clash can only be a server key.
- A server that cannot be reached is dropped for that turn. The turn goes on.
- Only the hub's main agent gets personal servers. Delegates do not.
- The shared agent, options and registry are never changed. Clients are
  opened and closed inside the turn.

### Connector capability (`connector_<app>`)

Implemented. Each OAuth MCP server is a connector app named by its server ID
(the ID typed in OWUI when registering it, lowercased, other characters
turned into `_`). A server registered as `gmail` is the app `gmail` and the
capability `connector_gmail`.

- **Managed hubs** (access managed in the Console): a personal server is
  injected only when the caller holds `connector_<app>` in that hub. Grant it
  like any other capability.
- **Legacy hubs** (Open WebUI groups): injection is unchanged, apart from the
  surface rule above. Starting a connection from chat (below) needs an Open
  WebUI group named `connector_<app>`.
- If the access store cannot be read, no personal server is injected that turn.

## Connect from chat (connection journey)

Implemented, off by default. A person asks the agent to connect an app (for
example "connect my Gmail") in web chat or WhatsApp. The agent sends a
personal link. The person approves access in the browser, a Hubzoid page shows
the verified result, and WhatsApp gets a confirmation.

The journey uses Open WebUI native MCP only: an app is connectable when an
OAuth 2.1 MCP server is registered for it in OWUI. The optional Composio
integration (`CONNECTIONS`, `COMPOSIO_API_KEY`) is unchanged and is not part of
the journey.

### Turn it on

In the hub's `.env`:

```dotenv
HUBZOID_CONNECT_JOURNEY=true
# HUBZOID_CONNECT_TTL=600   # link lifetime in seconds (60 to 3600)
```

The agent then has a `connect_account(app, reconnect=false)` tool on every
backend. It also needs:

- an app to connect: an OAuth 2.1 MCP server registered in OWUI, with
  `OWUI_NATIVE_MCP=true`. For any other app the tool says it is not available
  to connect on this hub.
- the `connector_<app>` capability for the person (Console grant on a managed
  hub, OWUI group of that name on a legacy hub).
- the surface in `HUBZOID_RESTRICTED_SURFACES`. Add `whatsapp` for WhatsApp.
- `WEBUI_URL` set to the public address people open (the link is
  `<WEBUI_URL>/portal/connect/<id>`).

### What happens

1. `connect_account` finds the one OWUI server for the app, checks the
   capability (surface first) and asks OWUI whether the person is connected
   already. If they are, the agent says so. Otherwise it gets a link to
   `/portal/connect/<id>`, never a provider URL.
2. The link page needs a signed-in OWUI session. The email of that session
   must be the person who asked. Anyone else gets "This link is for another
   account" (and the attempt is recorded in the access log).
3. **Continue** (a same-origin POST) re-checks a block or a revoked grant and
   sends the browser to OWUI's own authorize route, with a short-lived
   `hz_connect` cookie.
4. After consent the browser comes back to `/portal/connect/<id>/done`. The
   page asks OWUI whether this journey connected: a new `oauth_session` row
   for that person and server, created after the journey started, whose token
   is usable now. It never reads the parameters on the return URL.
5. The page shows **connected**, **not connected** or **finishing** (it checks
   `/status` for up to 30 seconds). WhatsApp gets its confirmation from the
   hub's inbound process (see [inbound surfaces](inbound-surfaces.md)).

Other outcomes: **Cancel** on the page, an **expired** link (the TTL), and a
**newer link** for the same app (the older one stops working). Asking with
`reconnect=true` replaces the existing connection. OWUI deletes the old session
itself.

**One server per app.** If two OWUI servers map to the same app, the tool
refuses and names both, so no one ends up with two connections. Remove one.

The connector capabilities a hub offers are listed by
`connect_journey.permissions(hub)` for the Console.

### Needs the edge rewrite (or falls back)

OWUI always sends the browser to its own home page after authorization. The
edge turns that redirect into `/portal/connect/<id>/done` while the
`hz_connect` cookie is present (the edge side lands separately). Without it the
person lands on the chat home page. The connection still works, WhatsApp still
gets its confirmation (the inbound process checks the provider), and asking the
agent again reports "already connected".

### Limits

- The WhatsApp confirmation and the YES continuation are WhatsApp only.
  Telegram and web chat get the link and the done page.
- OWUI's `/auth?redirect=` is offered on the sign-in page but is not yet
  verified against the pinned OWUI bundle. The page also says to open the link
  again after signing in.

### Check it with real accounts (manual)

Unit tests use fakes. Before relying on it, run once on a test deployment:

1. Register a Gmail MCP server that supports OAuth 2.1 in OWUI (ID `gmail`),
   with a Google OAuth client whose redirect URI is
   `<WEBUI_URL>/oauth/clients/mcp:gmail/callback`.
2. Set `OWUI_NATIVE_MCP=true`, `HUBZOID_CONNECT_JOURNEY=true` and add
   `whatsapp` to `HUBZOID_RESTRICTED_SURFACES`. Grant `connector_gmail`.
3. From a WhatsApp number in `identity/access.csv`, ask "connect my Gmail".
   Open the link while signed in as another account (expect 403), then as the
   right one. Approve in Google.
4. Expect the done page to say connected, one WhatsApp confirmation, and on
   YES the waiting request to run once. Check `oauth_session` has one row for
   that person and server.
5. Ask something that needs Gmail on each backend (`claude-local`, an OpenAI
   model, `codex-local`). The tool must answer with that person's mailbox.

For deployment and upgrade checks, see [administration](ADMINISTRATION.md) and
[upgrading](UPGRADING.md). Preserve saved tool-server connections when upgrading.
