# Hosted MCP server

Hubzoid exposes its tools and knowledge through Streamable HTTP with OAuth-only
authentication. The assistant supplies its model; Hubzoid supplies context,
tools and the existing access controls.

## Enable and connect

In the hub's `.env`:

```dotenv
MCP_SERVER=true
MCP_PUBLIC_URL=https://your-domain.example/mcp
HUBZOID_AUTH=true
```

Restart Hubzoid, then connect:

```sh
claude mcp add --transport http --scope user hubzoid https://your-domain.example/mcp
```

In Claude Code, run `/mcp` and authenticate. The browser opens the Hubzoid
sign-in page, then Hubzoid consent, then returns to Claude. An existing
sign-in skips the form, never the consent. Static API keys are not accepted.

See [the complete Claude setup guide](mcp-oauth-claude.md) for deployment,
gateway discovery, remote Claude connectors, migration and revocation. Other
clients must support OAuth with PKCE and Streamable HTTP.

## Identity & access

Hosted MCP accepts only Hubzoid-issued OAuth credentials. Signing in to
Hubzoid establishes identity and Hubzoid consent authorizes the connection.
Session cookies, Open WebUI (1.x) API keys and bridge keys cannot authenticate
MCP. Current account
status and hub permissions are checked on every request. Set `MCP_SERVER=false`
and restart to disable MCP.

Every tool call runs under the caller's identity on surface `mcp`, through
the same access guard as chat:

* The caller needs `use_hub` to connect, plus the named grant for each
  restricted capability. Saving shared knowledge (`remember`) is a chat
  tool and is not offered over MCP.
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
`.env` file. Accounts and OAuth grants live in the deployment's operational
database, which the gateway records in its deployment manifest. SQLite and
PostgreSQL are supported; a configured database failure denies access rather
than falling back to a local SQLite copy.

Separately managed bridges (`--no-bridges`, systemd) use the same deployment
manifest, or the same `DATABASE_URL`/`HUBZOID_OPERATIONAL_DB`.

A shared account does not grant entry to every hub. Grant
**Use this agent** in each intended hub; the same `use_hub` check protects chat
and MCP.

`MCP_ACCESS_GROUP` is no longer read: groups grant nothing, and
`hubzoid doctor` warns while it is set. See [access management](access-management.md).

## Operational notes

* Stateless Streamable HTTP: no sessions, safe behind load balancers and
  across bridge restarts; one POST per JSON-RPC call.
* Auth failures return 401 with OAuth discovery in `WWW-Authenticate`.
* Keep the exposed tool list curated: each tool schema uses client context.
* Revoke connections at `/mcp/oauth/connections` (under the hub prefix in a gateway).
* The hub access audit log records tool authorization decisions.
