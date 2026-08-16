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
Zoho, Odoo, ...), Hubzoid also picks up MCP servers a user connects natively in
Open WebUI - no Hubzoid UI involved:

1. An admin registers the MCP server in OWUI (Settings -> Admin -> External
   Tools), auth type OAuth 2.1.
2. A user opens `+ -> Integrations -> Tools`, enables it, and completes the
   OAuth redirect. OWUI vaults that user's token.
3. On the user's next turn the bridge reads and decrypts *their* token from
   OWUI's database and calls the MCP server as them. Two users reach the same
   server as themselves; the same connection works across surfaces (Slack,
   Telegram) once their identity maps to the same OWUI account.

Requirements: OWUI >= 0.6.31, and OWUI and the bridge must share the same
`WEBUI_SECRET_KEY` (OWUI encrypts the tokens with it, so `hubzoid run` already
gives both processes the value from your `.env`). On by default; set
`OWUI_NATIVE_MCP=0` to disable. A connected token is used until it expires;
automatic refresh is a tracked follow-up, so a user reconnects after expiry
for now.

Full design, the data path, and the OWUI-upgrade checklist:
[per-user-tool-connections.html](per-user-tool-connections.html).
