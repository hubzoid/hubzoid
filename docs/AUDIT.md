# Audit log and security monitoring

Hubzoid can record every tool call, sign-in event and admin change as one
structured, redacted record. A security team forwards the records to its SIEM
and alerts on them. Admins read them on the Console's Activity page and export
them for an investigation.

It is off by default. Turn it on in the deployment's environment (or a hub's
`.env`):

```
HUBZOID_AUDIT=both          # off | log | db | both
HUBZOID_LOG_FORMAT=json     # text | json, for every other log line
```

| Setting | Default | What it does |
|---|---|---|
| `HUBZOID_AUDIT` | `off` | `log`: one JSON line per event on standard output (logger `hubzoid.audit.<kind>`). `db`: a row in the database, shown on the Activity page and in the export. `both`: the two together. |
| `HUBZOID_AUDIT_ARGS` | `names` | What a tool call record keeps of its arguments. `names`: argument names, plus identifiers (paths, ids, names, web addresses without their query). `redacted`: the values after the credential scrub, with email addresses, phone numbers and card numbers masked, capped at 1,000 characters. |
| `HUBZOID_AUDIT_RETENTION_DAYS` | `90` | How long database rows are kept. |
| `HUBZOID_LOG_FORMAT` | `text` | `json` writes every log line as a JSON object with the hub, person, surface, chat, workflow run, client address and trace id. |

Every record and log line passes the same credential scrub Hubzoid uses for
stored tool results (`hubzoid/redaction.py`). Audit records never slow a tool
call: database rows are written in batches by a background thread.

## What is recorded

**Tool calls** (`kind: tool`, `event: tool.call`), from the one wrapper every
runtime's hub and MCP tools pass through, and from Claude's tool hooks for the
MCP servers Claude calls directly:

```json
{"ts": "2026-11-20T09:14:02.311Z", "kind": "tool", "event": "tool.call",
 "hub": "support", "subject": "priya@example.com", "surface": "web",
 "chat_id": "c_8f2", "client": "203.0.113.7",
 "tool": "read_ticket", "server": "zendesk", "decision": "allow", "outcome": "ok",
 "duration_ms": 412, "result_bytes": 5120, "args": {"ticket_id": "48213", "note": "…"}}
```

A refused call has `"decision": "deny"` and the `reason` (for example
`credentials_revoked`, `account_inactive`, `app_tool`, `permission`). A call
that raised has `"outcome": "error"` and the `error` class. A workflow's call
has `"subject": "workflow:<name>"` and its `run_id`.

**Sign-in events** (`kind: auth`): `auth.signed_in`, `auth.signed_out`,
`auth.sign_in_failed` (a wrong password for a known account),
`auth.attempt_failed` (every failed attempt the sign-in limits count: unknown
accounts, wrong codes and re-checks too, with the address),
`auth.locked_out` (with `locked_seconds`), `auth.api_key_created`,
`auth.api_key_revoked`, `auth.api_key_used` (a key's first use from an address
since the process started), `auth.app_token_used` (the same for a machine JWT
from an API app, with `key` set to `app:<id>`), and the two-step sign-in changes. Never a password,
code or key.

**Admin changes** (`kind: admin`): grants and removals, blocked accounts,
secrets set or deleted (the name, never the value), workflow run controls and
settings changes, with the `actor`, `target`, `target_hub` and a short summary.

**Restricted-tool decisions** (`kind: access`) keep their own table, as before,
and are part of the export.

## Activity page

Console, Activity: four tabs. **Access changes** and **Tool decisions** are as
before. **Tool calls** and **Sign-in events** list what `HUBZOID_AUDIT=db` (or
`both`) stored, with filters for agent, time, person, allowed or denied,
finished or failed, tool and channel. Sign-in events are for organization
administrators. **Export** downloads what the current tab holds for the chosen
agent and time.

## Export

For a backfill into a SIEM or an investigation, oldest first, one JSON object
per line, the same records the log lines carry:

```
hubzoid audit ./support --export --since 2026-11-01 --until 2026-11-20 --out audit.jsonl
hubzoid audit ./support --export --all-hubs --kind tool,auth
```

Or over the Console API with an admin's session or personal API key:

```
GET /portal/api/audit/export?since=2026-11-01T00:00:00Z&kind=tool,access,admin,auth&hub=support
```

`kind` is any of `tool`, `access`, `admin`, `auth`. Admin and sign-in events
need an organization administrator. An export holds at most `limit` records
(default 100,000 over the API, 1,000,000 from the command). When more remain,
the last line is `{"kind": "export", "event": "export.truncated", "next_since":
...}`: export again from that time (records at that instant may repeat).

Audit lines go through a bounded queue to standard output, so a slow log driver
never holds up a tool call. If the queue fills, lines are dropped and the drop
is reported on standard error. Queued lines and rows are written when the
process stops gracefully.

## What a security team can alert on

Each of these is a filter on the audit log lines, so it can be a CloudWatch
metric filter or a SIEM rule with no Hubzoid code.

| Signal | Records | Why |
|---|---|---|
| A spike in refused tool calls for one person or one tool | `tool.call` with `decision = deny` | Probing for access, or a prompt injection naming tools |
| Any tool call refused because an account was removed or its sign-in revoked | `reason` in `account_inactive`, `credentials_revoked`, `credential_ended` | A session or key that should be dead |
| Many reads or large results for one person in a short time | `tool.call` by `subject`, `result_bytes` | Possible data exfiltration |
| A person's first use of a new MCP server, connector or restricted tool | `tool.call` `server` | A change in scope worth a look |
| Repeated sign-in failures or lockouts from one address | `auth.attempt_failed`, `auth.locked_out` by `client` | Password spraying, account takeover |
| An API key or machine token used from a new address | `auth.api_key_used`, `auth.app_token_used` | A leaked key or a misused issuer |
| Grants given, secrets set or deleted | `admin.grant`, `admin.secret_set`, `admin.secret_delete` | Privilege changes |
| Tool error rate | `tool.call` with `outcome = error` | A broken integration or abuse |

CloudWatch Logs metric filter for refused tool calls:

```
{ $.kind = "tool" && $.decision = "deny" }
```

and for lockouts:

```
{ $.event = "auth.locked_out" }
```

## Telemetry

OpenTelemetry stays opt-in ([OBSERVABILITY.md](OBSERVABILITY.md)).
`HUBZOID_OTEL_SIGNALS` chooses the agent runtime's signals (traces, metrics,
logs) and `HUBZOID_OTEL_CONTENT` (`none`, `tools`, `full`) is the most prompt
and tool content they may carry. When a tracer is set up (run Hubzoid under
`opentelemetry-instrument`), every tool call is also an `execute_tool` span
with the GenAI attribute names (`gen_ai.tool.name`, `gen_ai.tool.call.id`,
`error.type`). Audit records are never sampled.
