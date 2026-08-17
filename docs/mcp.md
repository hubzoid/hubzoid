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
   server as themselves; the connection follows their identity across surfaces
   (Slack, Telegram) once it maps to the same OWUI account.

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
- **Token refresh** is a tracked follow-up: a connected token is used until it
  expires, then the user reconnects. (The two OAuth consumers - OWUI and the
  bridge - sharing one rotating refresh token needs deliberate design.)
- **Turn it off:** remove `OWUI_NATIVE_MCP` (or set it to `0`).

Full design, the exact data path, and the OWUI-upgrade checklist:
[per-user-tool-connections.html](per-user-tool-connections.html).
