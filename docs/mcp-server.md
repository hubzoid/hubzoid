# Hosted MCP server — bring your own intelligence

A hub can serve its tools and knowledge to **external MCP clients** — Claude
Code, Cursor, any Streamable-HTTP MCP client. The caller brings their own
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

## Identity & access

The Bearer token is resolved **read-only against Open WebUI's own database**
(`api_key` table → user email → OWUI groups). Revocation = delete the key in
OWUI; expiry is honored; disabling `ENABLE_API_KEYS` kills the whole surface.

Every tool call runs under the caller's identity on surface `mcp`, through
the same access guard as chat:

* unrestricted tools: available to any authenticated caller,
* `restricted/<perm>.py` tools: require the caller to be in the OWUI group
  `<perm>` — same rule as the web UI, and hidden from `tools/list` otherwise
  (the invoke-time guard fails closed regardless, and every decision lands
  in the audit log),
* `BRIDGE_API_KEYS` are **never** accepted on `/mcp` — the bridge key is
  infrastructure trust, not a user.

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
name: IRS Hub
mcp_instructions: |
  Tools and knowledge for IRS donation reconciliation. Prefer grep_data
  for raw ledger lookups; read_knowledge for policy documents.
---
```

## Gateway mode — set MCP_ACCESS_GROUP

Each MCP-enabled hub gets its own endpoint: `https://<host>/b/<slug>/mcp`.
Detection is strictly per-hub: `MCP_SERVER=true` must be in **that hub's**
`.env` file. Bridges run separately from the gateway (`--no-bridges`,
systemd) must set `HUBZOID_OWUI_DB=<gateway-data>/webui.db` in their
environment so key/group lookups read the shared user database.

**Important:** in gateway mode one shared user database backs every hub, and
a minted API key belongs to the *user*, not to a team. Without a per-hub
gate, any logged-in user of any team can reach an MCP hub's **unrestricted**
tools and knowledge — the chat UI's per-model ACLs do not apply here. Gate
each hub's whole MCP surface on an OWUI group:

```dotenv
# IRSHub/irs-hub/.env
MCP_SERVER=true
MCP_ACCESS_GROUP=irs        # only members of the OWUI group "irs" get past auth
```

Non-members get 401 before seeing a single tool name. Single-hub
deployments can usually leave it unset (everyone in that OWUI *is* the
team).

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
