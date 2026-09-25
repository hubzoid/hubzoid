# Hosted MCP server — bring your own intelligence

A hub can serve its tools and knowledge to **external MCP clients** — Claude
Code, Codex, Hermes, Cursor, and other Streamable-HTTP MCP clients. The caller brings their own
model (their subscription, their harness, their context); the hub provides
curated tools, org knowledge, and per-role access control. The inverse of
`connectors/.mcp.json`, which is the hub *consuming* MCP servers.

## Enable

```dotenv
# <hub>/.env
MCP_SERVER=true
```

That's it. On the next `hubzoid run` (or gateway restart):

* the bridge serves MCP at `/mcp` (Streamable HTTP, stateless),
* the edge exposes it publicly — `https://<host>/mcp` for a single hub,
  `https://<host>/b/<hub>/mcp` per hub in gateway mode,
* Open WebUI gains per-user API keys (Settings → Account → API keys) so
  users can mint their own credential. The keys are locked to **deny-all
  inside OWUI** (endpoint restrictions with an empty allowlist), so they are
  identity credentials for the MCP surface only.

`/v1` stays loopback-only, exactly as before. MCP is the only bridge surface
the edge exposes beyond artifact downloads.

## Connect (what your users do)

1. Log into the hub's Open WebUI → Settings → Account → API keys → create a
   key (`sk-...`).
2. ```bash
   claude mcp add --transport http myhub https://hub.example.com/mcp \
     --header "Authorization: Bearer sk-..."
   ```

Their agent now has the hub's tools (`read_knowledge`, `grep_data`, hub-local
tools, …) and, at connect time, receives the hub's instructions so it knows
what the hub is and how to use them.

### Codex

Add to `~/.codex/config.toml` (or the relevant Codex project configuration):

```toml
[mcp_servers.hubzoid]
url = "https://hub.example.com/mcp"
bearer_token_env_var = "HUBZOID_MCP_TOKEN"
```

Set `HUBZOID_MCP_TOKEN` in the environment that launches Codex to the user's
Open WebUI API key. No custom plugin is required. See the
[official Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

### Hermes

Add to Hermes' `config.yaml`:

```yaml
mcp_servers:
  hubzoid:
    url: "https://hub.example.com/mcp"
    headers:
      Authorization: "Bearer ${HUBZOID_MCP_TOKEN}"
```

Store the API key in Hermes' secret environment. This uses static bearer
credentials, so do not enable OAuth for this connection. See the
[official Hermes MCP reference](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference).

These clients support Hubzoid's transport/authentication contract. Hubzoid's
regressions exercise MCP HTTP calls and token revocation; they do not claim
end-to-end testing of every client version. A plugin could package setup and
instructions later, but is not needed to connect.

## Identity & access

The Bearer token is resolved **read-only against Open WebUI's own database**
(`api_key` table → user identity), on SQLite or PostgreSQL. It is a **per-user
API key**, not the browser login JWT or a bridge secret. Deleting the key revokes
MCP access; expiry, pending approval and suspended accounts are enforced.
Set `MCP_SERVER=false` and restart to remove the MCP surface entirely.

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
optional `DATABASE_SCHEMA` in its deployment manifest. Key, group and OAuth
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
MCP_ACCESS_GROUP=sales
```

After that hub's permissions become authoritative in Hubzoid, direct grants
replace this legacy group gate. Do not rely on an Open WebUI group to grant or
revoke managed-hub access. See [access management](access-management.md).

## Operational notes

* Stateless Streamable HTTP: no sessions, safe behind load balancers and
  across bridge restarts; one POST per JSON-RPC call.
* Auth failures are 401 with `WWW-Authenticate` (OAuth-ready for a later
  claude.ai-connector phase); claude.ai custom connectors require OAuth and
  are not supported by this token phase.
* Keep the exposed tool list curated — every tool schema spends context
  tokens in every connected client.
* MCP use does not touch OWUI's `last_used_at` on the key (the lookup is
  read-only by design), so OWUI's key list won't reflect MCP activity — the
  hub's access audit log is the source of truth for who called what.
