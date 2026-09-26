---
name: mcp-and-connectors
description: How MCP servers and Composio plug into a Hubzoid hub. The integration layer.
keywords: [mcp, connectors, composio, integration, tools, oauth]
---

# MCP and connectors

A hub talks to the outside world through three layers, from least to
most powerful.

1. **Pre-shipped tools.** `http_get`, `web_search`, file reads,
   knowledge and skill loads. Generic. Available in every hub.
2. **Custom `tools_local/`.** Your own Python functions decorated with
   `@function_tool`. Auto-discovered at boot. Run in the same process.
3. **MCP servers.** External tool catalogs exposed over the Model Context
   Protocol. Configured in `connectors/.mcp.json`. Loaded at boot.

MCP is the right answer for anything that needs OAuth (Gmail, Slack,
HubSpot, Notion, Salesforce, etc.). Do not build OAuth flows inside your
hub.

## The `.mcp.json` shape

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["@modelcontextprotocol/server-filesystem", "./workspace"]
    },
    "github": {
      "command": "npx",
      "args": ["@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GH_TOKEN}"}
    }
  }
}
```

`${VAR}` references in any string field resolve against the process
environment at boot.

## Composio for OAuth-managed tools

For anything that requires user OAuth (Gmail, Slack, Salesforce, HubSpot,
50 other enterprise apps), the recommended path is Composio's MCP server.

1. Create a Composio account at `composio.dev`.
2. Authorize the apps you need in Composio's dashboard. OAuth flows live
   there. Composio holds refresh tokens and renews them silently.
3. Put your Composio API key in `.env`: `COMPOSIO_API_KEY=...`.
4. Add one entry to `connectors/.mcp.json` pointing at Composio's MCP
   endpoint.

Your hub now has access to every app you authorized, no OAuth code
required, no token refresh code required.

## What all three runtimes share

The same `connectors/.mcp.json` is read by all three runtimes: the OpenAI
Agents SDK with LiteLLM models, the Claude Agent SDK (`claude-local`), and
local Codex (`codex-local`). Switch `MODEL=` and your integrations keep
working.

## Connectors versus serving MCP

`connectors/.mcp.json` is the hub consuming MCP servers. The reverse also
exists: a hub can serve its own tools and knowledge to a personal
assistant such as Claude Code or Codex. Opt in with `MCP_SERVER=true` in
`.env`. Each user connects with their own credential, and the hub's
access rules apply.

## Hub-wide versus per-user

- Servers in `connectors/.mcp.json` are hub-wide. Every user of the hub
  reaches them with the same credential.
- For tools where each user must act as themselves, an operator can opt
  in to per-user MCP servers registered in Open WebUI
  (`OWUI_NATIVE_MCP=true`). Each user connects their own account there.
  See `docs/mcp.md` in the repository before enabling it.
- Hubzoid does not store `.mcp.json` OAuth tokens. They live wherever the
  MCP server holds them (Composio, an external service, a local file).
