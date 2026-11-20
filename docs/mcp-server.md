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

Chat-scoped tools (`write_artifact`, `make_excel`, `make_pdf`, `make_word`, `read_upload`, …) are not exposed —
they need a live chat to resolve their directories. Model-delegates are not
exposed either: an MCP caller brings their own model; the hub does not spend
inference for them.

## Connected apps

The apps a person connects in Settings ([connectors](mcp.md)) reach their
assistant too. On every `tools/list` the hub asks which apps this caller may
use in this agent, with the same check as chat: the connector is offered here,
the caller holds `connector_<id>`, and they connected it (or it uses a shared
key). Each app's tools follow the hub's own, named `my_<id>__<tool>`:

| In the app | Over hosted MCP | In Claude Code |
|---|---|---|
| `whoami` | `my_crm__whoami` | `mcp__<server name>__my_crm__whoami` |

A call goes to the app as the caller, with their own token (refreshed when
due) or the connector's shared key, after the same checks as a hub tool and as
chat:

* the caller's account, MCP grant and `use_hub`: a stopped account or an ended
  grant is refused;
* the connector grant and the admin's tool allow-list, checked again on the
  call, not only when listing;
* the hub's approval rules (`hitl.yaml`), matched with the app's own tool name
  and the server key `my_<id>`, as in chat. A call waiting for a person is not
  forwarded;
* a row in the access log on surface `mcp`. A call whose row cannot be written
  is not forwarded.

What the app returns comes back as it sent it: content, structured content and
the error flag. A call whose outcome is unknown (the app stopped answering) is
not retried.

**Reconnect after connecting an app.** The endpoint is stateless and sends no
notice when a person's tools change, so an assistant sees a newly connected
app once it lists tools again: in Claude Code, `/mcp`, then Reconnect.
`connect_account` says so when it is called over MCP.

* A character MCP tool names may not hold becomes `_` and the name ends in a
  short hash; a name over 128 characters is cut and ends in a hash. The call
  always goes to the app's own name. Assistants may limit names further (Claude
  counts the whole `mcp__<server>__<tool>`), so keep connector IDs short.
* A name the hub serves stays the hub's tool: an app tool that would take it is
  left out, as is a name two app tools would share.
* An app's output schema is not passed on. The hub sometimes answers for the
  app (an approval waiting, a refusal), which would not match it. The app's
  structured content still is.
* Descriptions come from the app, labelled with the connector's name: treat
  them as the app's text.
* Each app's tool list is cached for 60 seconds per person and credential. An
  app that does not answer within 10 seconds is left out of that listing, and
  a listing waits at most 20 seconds for all of a person's apps.
* Tools only: an app's resources, prompts and other MCP features are not
  passed on.
* Forwarding never runs the hub's model. An approval rule with `ask: auto`
  asks Hubzoid's decision model, as it does in chat.

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
