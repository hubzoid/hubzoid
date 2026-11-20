# Hosted MCP server

Hubzoid can serve an agent over MCP, so Claude Code, Claude web or another
assistant uses the agent's knowledge, tools and workflows with its own model.
Use it when people already work in an assistant and want the agent there too.
Sign-in is OAuth only, and every call runs as the person, with the same access
as chat.

## Quick start

`hubzoid run` (without `--no-ui`) turns hosted MCP on when the public address
is HTTPS or this computer, and prints the command to connect Claude Code. For a hosted hub, set
it in the hub's `.env`:

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
sign-in skips the form, never the consent. For proxies, gateways, Claude web
and revocation, see [Connect Hubzoid MCP to Claude](mcp-oauth-claude.md).
Other clients need OAuth with PKCE and Streamable HTTP.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `MCP_SERVER` | `false`. `hubzoid run` sets `true` when the public address is HTTPS or loopback, unless the key is already set | Serves the agent at `/mcp`. In a gateway it must be in that hub's own `.env`. |
| `MCP_PUBLIC_URL` | `hubzoid run`: `<public address>/mcp`. Gateway: `<base>/b/<slug>/mcp` | The exact public URL, ending in `/mcp`. HTTPS is required. Plain HTTP works only on `localhost`, `127.0.0.1` and `::1`. Changing it means every assistant reconnects. |
| `MCP_STATELESS` | `false` | Serves each request on its own, with no session. Use it behind several replicas without sticky sessions. Questions and approvals then fall back to the assistant asking. |
| `HUBZOID_MCP_SIGNING_KEY` | unset | A PEM RSA private key that signs access tokens. When set, the database keys are not used. |
| `HUBZOID_MCP_SIGNING_KEYS_PREVIOUS` | unset | Older PEM keys that still verify, used with `HUBZOID_MCP_SIGNING_KEY` during a rotation. |

`hubzoid run` treats a key already set in the hub's `.env` or the environment
as explicit, even when empty. Set `MCP_SERVER=false` and restart to turn MCP
off.

## What an assistant can do, next to chat

The assistant gets the same tools as the agent's chat, under the same grants.
Tools a person may not use are hidden from `tools/list` and checked again on
every call.

| In chat | Over hosted MCP |
|---|---|
| Files (`write_artifact`, `make_excel`, `make_pdf`, `make_word`, `list_artifacts`) | The same tools, in the person's own file space. Each result has the download link and an MCP `resource_link` (`hubzoid-file://artifacts/<name>`) the assistant can read back. Only the person who made a file can read it. |
| Attach a file | `upload_file`, as text or base64, up to 2.5 MB (or the hub's upload limit when lower). `read_upload`, `read_upload_full` and the hub's own tools then read it. Use the web app for larger files. |
| A follow-up question (`ask_question`) | Asked through MCP elicitation when the client supports it (Claude Code does), and the answer comes back in the result. Otherwise the assistant is told to ask the person itself. The person has ten minutes to answer a form. |
| An approval card | When the caller may approve it and the client supports elicitation, it is asked in place and recorded as their decision, then the call runs once. Otherwise the result says where to approve, and calling again with the same arguments then runs it once. Arguments too long for a form, and approvals that need someone else, wait in the Inbox. |
| `remember`, hub changes | The same tools and gate as chat: **Save shared knowledge** or managing the hub, with in-app editing on. A manager's knowledge change applies. Anyone else's is reviewed in Settings, Hub changes. |
| Core skills such as `dashboard` | Listed by `list_skills`, as in chat. |
| Sub-agents with their own `model:` | `handover_<name>`, only for people with **Use sub-agents on this agent's model from an assistant** (`mcp_delegates`). The hub's runtime runs once as the caller and hands the task to that sub-agent, so the agent pays for those model calls. A run stops after ten minutes. Without the grant the sub-agent is still offered as a skill for the assistant's own model. |
| Workflows | `list_workflows`, `workflow_runs`, `run_workflow`, `pause_workflow`, `resume_workflow`, `cancel_workflow_run`, each "Run for me" tool, and personal schedules (`schedule_workflow`, `my_schedules`, `change_schedule`, `my_reports`), under the same workflow access. A question or approval from a run started here is asked in place too. |
| The Inbox | `my_requests` lists the approvals and questions waiting for the person on this agent, with the Inbox link. It never decides. |

Only over MCP: `agent_health`, `eval_status` and `recent_denials` for people
who manage the agent ([diagnostics](diagnostics.md)), and
[cards](cards.md) when the hub has `mcp-apps.yaml`.

Not over MCP: `record_feedback` rates the hub's answer in a chat, and over MCP
the answer is the assistant's own. The hub's own `connectors/.mcp.json` servers
are not passed on.

## Access

Hosted MCP accepts only Hubzoid-issued OAuth credentials. Session cookies,
Open WebUI (1.x) API keys, `BRIDGE_API_KEYS` and other static keys are refused.

* Signing in establishes identity. Hubzoid consent authorizes the connection.
* The caller needs **Use this agent** (`use_hub`), plus the named grant for
  each restricted capability. A shared account does not reach every agent.
  Grant access in each one.
* Account status and grants are read on every call. A blocked account or a
  revoked connection stops on the next call.
* Every call runs on surface `mcp` through the same access guard as chat, and
  the access log records each decision.
* `MCP_ACCESS_GROUP` is no longer read. Groups grant nothing, and
  `hubzoid doctor` warns while it is set. See
  [access management](access-management.md).

## Connected apps

The apps a person connects in Settings ([connectors](mcp.md)) reach their
assistant too, named `my_<id>__<tool>`:

| In the app | Over hosted MCP | In Claude Code |
|---|---|---|
| `whoami` | `my_crm__whoami` | `mcp__<server name>__my_crm__whoami` |

On every `tools/list` the hub works out which apps this caller may use in this
agent, with the same check as chat: the agent offers the connector, the caller
holds `connector_<id>`, and they connected it (or it uses a shared key). A call
goes to the app as the caller, with their own token (refreshed when due) or the
connector's shared key, after these checks:

* the caller's account, MCP grant and `use_hub`. A stopped account or an ended
  grant is refused.
* the connector grant and the admin's tool allow-list, checked again on the
  call.
* the hub's approval rules (`hitl.yaml`), matched with the app's own tool name
  and the server key `my_<id>`, as in chat. A call waiting for a person is not
  forwarded.
* a row in the access log on surface `mcp`. A call whose row cannot be written
  is not forwarded.

The app's answer comes back as it sent it: content, structured content and the
error flag. A call whose outcome is unknown (the app stopped answering) is not
retried.

**Reconnect after connecting an app.** The endpoint sends no notice when a
person's tools change. The assistant sees a new app the next time it lists
tools: in Claude Code, `/mcp`, then Reconnect. `connect_account` says so when
it is called over MCP.

Limits:

* A character MCP tool names may not hold becomes `_`, and the name ends in a
  short hash. A name over 128 characters is cut and ends in a hash. The call
  always goes to the app's own name. Assistants may limit names further (Claude
  counts the whole `mcp__<server>__<tool>`), so keep connector IDs short.
* A name the hub serves stays the hub's tool. An app tool that would take it is
  left out, as is a name two app tools would share.
* An app's output schema is not passed on, because the hub sometimes answers
  for the app (an approval waiting, a refusal). Its structured content still is.
* Descriptions come from the app, labelled with the connector's name. Treat
  them as the app's text.
* Each app's tool list is cached for 60 seconds per person and credential. An
  app that does not answer within 10 seconds is left out of that listing, and a
  listing waits at most 20 seconds for all of a person's apps.
* Tools only. An app's resources, prompts and other MCP features are not passed
  on.
* Forwarding never runs the hub's model. An approval rule with `ask: auto` asks
  Hubzoid's decision model, as it does in chat.

## Set the instructions

The initialize response carries the hub's `AGENTS.md` body as MCP server
instructions. Claude Code adds them to the connecting agent's context. If
`AGENTS.md` holds internal-only guidance or is long, write a version for
assistants in the frontmatter. It replaces the body when present:

```yaml
---
name: Sales Hub
mcp_instructions: |
  Tools and knowledge for sales order reconciliation. Prefer grep_data
  for raw ledger lookups and read_knowledge for policy documents.
---
```

Keep the tool list focused too. Every tool schema uses the client's context.

## Gateway mode

Each MCP-enabled hub gets its own endpoint, `https://<host>/b/<slug>/mcp`.
Only `MCP_SERVER=true` in **that hub's** `.env` turns it on. Accounts and OAuth
grants live in the deployment's operational database, which the gateway
records in its deployment manifest. SQLite and PostgreSQL are supported. If the
configured database fails, access is denied. Hubzoid never falls back to a
local SQLite copy.

Separately managed bridges (`--no-bridges`, systemd) use the same deployment
manifest, or the same `DATABASE_URL` or `HUBZOID_OPERATIONAL_DB`.

## Access tokens and outside verifiers

Access tokens are JWTs ([RFC 9068](https://www.rfc-editor.org/rfc/rfc9068)):
`typ: at+jwt`, signed RS256 with a key of the deployment, valid for ten
minutes. Anything that reads the issuer's metadata can check them, for example
a load balancer or the AWS AgentCore JWT authorizer.

| What | Where |
|---|---|
| Issuer (`iss`) | `<MCP_PUBLIC_URL>/oauth` |
| Audience (`aud`) | `MCP_PUBLIC_URL` |
| Authorization server metadata, with `jwks_uri` | `/.well-known/oauth-authorization-server/<path of the issuer>` |
| OpenID-style discovery (same fields, for verifiers that only read this) | `<issuer>/.well-known/openid-configuration` |
| Public keys | `<issuer>/jwks` |

Claims: `sub` (the Hubzoid account id), `client_id`, `scope` (`hub:access`),
`iat`, `exp`, `jti` and `hz_grant` (the connection). The token holds no email
or name. Hubzoid still checks the connection and the person's access on every
call, so a revoked connection stops on the next call, not when the token
expires. Refresh tokens stay opaque and are replaced on every use. Hubzoid
issues no ID tokens.

### Signing keys

There is one key set per deployment. By default Hubzoid creates the first key
on first use and keeps it in the operational database, encrypted with the
deployment key. Back the two up together. To manage the key yourself, set
`HUBZOID_MCP_SIGNING_KEY` to a PEM RSA private key, for example from the
secret store or AWS Secrets Manager.

### Rotate signing keys

```sh
hubzoid mcp signing-keys list   --hub <hub-folder>
hubzoid mcp signing-keys rotate --hub <hub-folder>         # the old key verifies 11 more minutes
hubzoid mcp signing-keys rotate --hub <hub-folder> --now   # the old key stops verifying at once
```

`--hub` takes any hub of the deployment and defaults to the current folder.
`list` shows each key as `signs`, `verifies only` or `retired`. Other bridges
pick up a rotation within 30 seconds. After `--now`, connected assistants
refresh their tokens. An outside verifier that cached the public keys may
accept an old token until it expires (ten minutes at most).

With `HUBZOID_MCP_SIGNING_KEY` set, `rotate` refuses. Rotate by moving the
current PEM to `HUBZOID_MCP_SIGNING_KEYS_PREVIOUS`, setting a new one and
restarting.

### On AWS AgentCore

With `HUBZOID_MCP_HOST=agentcore` (`hubzoid deploy init aws`, `McpHost=agentcore`)
the hub's MCP server also runs as an AgentCore MCP runtime, each MCP session
in its own microVM. People connect Claude to the runtime's address, which ends
in `/invocations?qualifier=DEFAULT`, and sign in with this hub's own sign-in.

- AgentCore's JWT authorizer reads the metadata above (`discoveryUrl`) and
  accepts only tokens from this hub's issuer (`iss`). Claude registers a new
  client id per install, so `allowedClients` does not fit, and the runtime's
  own address cannot be its audience in a CloudFormation stack.
- Hubzoid then checks each token as on `/mcp`: the person's live grant, and
  that it was issued for the runtime's address. A token for `/mcp` is refused
  there, and a token for the runtime is refused on `/mcp`.
- AgentCore forwards `Authorization` (`requestHeaderAllowlist`). Its health
  check is an unsigned `ping`, which is the only call answered without a token.

Verified on AWS on 20 November 2026: sign-in, `tools/list`, a knowledge call,
and both refusals. See [AWS](deploy/aws.md#mcp-on-agentcore).

## Sessions

Each connected assistant gets a Streamable HTTP session, which elicitation
needs. The MCP SDK ends a session after 30 idle minutes, caps the number of
sessions and binds each one to the token that opened it. A bridge restart ends
every session, and the client opens a new one. `MCP_STATELESS=true` turns
sessions off.

## Troubleshooting

* **401 on every call.** The response carries OAuth discovery in
  `WWW-Authenticate`. Authenticate again (`/mcp` in Claude Code). Remove any
  static `Authorization` header from the client's configuration.
* **A tool is missing.** The person lacks its grant, or it is a chat-only tool.
  Grant it in Settings, then reconnect. Reconnecting never bypasses a grant.
* **A connected app is missing.** Reconnect the assistant after connecting the
  app. Check the `connector_<id>` grant and the app's allow-list.
* **No form for a question or approval.** The client does not support
  elicitation, or `MCP_STATELESS=true` is set. The assistant asks instead.
* **Revoke a connection.** Open `/mcp/oauth/connections` (under `/b/<slug>` in
  a gateway).
