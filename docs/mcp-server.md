# Hosted MCP server

Hubzoid exposes its tools and knowledge through Streamable HTTP with OAuth-only
authentication. The assistant supplies its model; Hubzoid supplies context,
tools and the existing access controls.

## Enable and connect

In the hub's `.env`:

```dotenv
MCP_SERVER=true
MCP_PUBLIC_URL=https://your-domain.example/mcp
WEBUI_AUTH=true
```

Restart Hubzoid, then connect:

```sh
claude mcp add --transport http --scope user hubzoid https://your-domain.example/mcp
```

In Claude Code, run `/mcp` and authenticate. The browser opens Open WebUI login,
then Hubzoid consent, then returns to Claude. Existing login sessions skip the
login form. Static API-key support and legacy/dual modes have been removed.

See [the complete Claude setup guide](mcp-oauth-claude.md) for deployment,
gateway discovery, remote Claude connectors, migration and revocation. An
optional [Claude Code plugin](../plugins/claude/hubzoid/README.md) packages this
connection; it does not replace login or consent. Other clients must support
OAuth with PKCE and Streamable HTTP.

## Identity & access

Hosted MCP accepts only Hubzoid-issued OAuth credentials. Open WebUI login
establishes identity and Hubzoid consent authorizes the connection. Static OWUI
API keys, login JWTs and bridge keys cannot authenticate MCP. Current account
status and hub permissions are checked on every request. Set `MCP_SERVER=false`
and restart to disable MCP.

Every tool call runs under the caller's identity on surface `mcp`, through
the same access guard as chat:

* On a Console-managed hub, the caller needs `use_hub` to connect, plus the
  named grant for each restricted capability. The built-in `remember` tool
  requires `curator`, shown in Console as **Save shared knowledge**.
* Unmigrated hubs retain their legacy group/roster permissions. Their optional
  `MCP_ACCESS_GROUP` also gates entry to the entire endpoint.
* Restricted tools are hidden from `tools/list` when unavailable and checked
  again on invocation. The audit log records access decisions.
* `BRIDGE_API_KEYS` are **never** accepted on `/mcp`.

Chat-scoped tools (`write_artifact`, `read_upload`, …) are not exposed —
they need a live chat to resolve their directories. Model-delegates are not
exposed either: an MCP caller brings their own model; the hub does not spend
inference for them.

## Instructions

The initialize response carries the hub's `AGENTS.md` body as MCP server
instructions (Claude Code injects them into the connecting agent's context).
If your AGENTS.md contains internal-only guidance or is long, provide an
external-safe version in frontmatter — it wins when present:

```yaml
---
name: Sales Hub
mcp_instructions: |
  Tools and knowledge for sales order reconciliation. Prefer grep_data
  for raw ledger lookups; read_knowledge for policy documents.
---
```

## Gateway mode

Each MCP-enabled hub gets its own endpoint: `https://<host>/b/<slug>/mcp`.
Detection is strictly per-hub: `MCP_SERVER=true` must be in **that hub's**
`.env` file. The gateway registers the shared Open WebUI database URL and
optional `DATABASE_SCHEMA` in its deployment manifest. Account, group and OAuth
lookups support SQLite and PostgreSQL; a configured database failure denies
access rather than falling back to a local SQLite copy.

Separately managed bridges (`--no-bridges`, systemd) should use the same
deployment manifest, or the same `DATABASE_URL`/`DATABASE_SCHEMA` as Open WebUI.
For SQLite without a manifest, set `HUBZOID_OWUI_DB=<gateway-data>/webui.db`.
Keep that path in separate bridges for shared uploads even with PostgreSQL;
`DATABASE_URL` takes precedence for database lookups.

A shared account does not grant entry to every Console-managed hub. Grant
**Use this agent** in each intended hub; the same `use_hub` check protects chat
and MCP. Open WebUI model visibility is not the MCP authorization boundary.

For an **unmigrated** gateway, set the legacy entry group on each hub:

```dotenv
MCP_SERVER=true
MCP_PUBLIC_URL=https://your-domain.example/b/sales/mcp
WEBUI_AUTH=true
MCP_ACCESS_GROUP=sales
```

After that hub's permissions become authoritative in Hubzoid, direct grants
replace this legacy group gate. Do not rely on an Open WebUI group to grant or
revoke managed-hub access. See [access management](access-management.md).

## Operational notes

* Stateless Streamable HTTP: no sessions, safe behind load balancers and
  across bridge restarts; one POST per JSON-RPC call.
* Auth failures return 401 with OAuth discovery in `WWW-Authenticate`.
* Keep the exposed tool list curated: each tool schema uses client context.
* Revoke connections at `/mcp/oauth/connections` (under the hub prefix in a gateway).
* The hub access audit log records tool authorization decisions.
