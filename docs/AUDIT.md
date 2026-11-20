# Audit log

The audit log records every tool call, sign-in event and admin change as one
structured, redacted record. Use it to forward events to a SIEM, alert on
unusual activity and look back during an investigation. It is off by default.

```bash
# in the deployment's .env, or a hub's .env
HUBZOID_AUDIT=both          # off | log | db | both
HUBZOID_LOG_FORMAT=json     # every other log line as JSON too
```

Restart Hubzoid after changing these. Then read the records on the Console's
**Activity** page, or export them:

```bash
hubzoid audit ./support --export --since 2026-11-01 --out audit.jsonl
```

## Settings

| Setting | Default | What it does |
|---|---|---|
| `HUBZOID_AUDIT` | `off` | Where records go. `log`: one JSON line per event on standard output (logger `hubzoid.audit.<kind>`). `db`: a row in the database, shown on the Activity page and in the export. `both`: the two together. |
| `HUBZOID_AUDIT_ARGS` | `names` | What a tool call record keeps of its arguments. `names`: argument names, plus identifiers (paths, ids, names, web addresses without their query). `redacted`: the values after the credential scrub, with email addresses, phone numbers and card numbers masked, capped at 1,000 characters. |
| `HUBZOID_AUDIT_RETENTION_DAYS` | `90` | How many days database rows are kept. |
| `HUBZOID_LOG_FORMAT` | `text` | `json` writes every log line as a JSON object with the hub, person, surface, chat, workflow run, client address and trace id. |

Every record and log line passes the same credential scrub Hubzoid uses for
stored tool results (`hubzoid/redaction.py`). With `HUBZOID_AUDIT=off`,
restricted-tool decisions and admin changes still keep their own table, as
before.

Audit records never slow a tool call. Database rows are queued and written in
batches by a background thread. Log lines go through a bounded queue to
standard output, so a slow log driver never holds up a tool call. If a queue
fills, records are dropped and the drop is reported on standard error. Queued
lines and rows are written when the process stops gracefully.

## What is recorded

### Tool calls

`kind: tool`, `event: tool.call`. They come from the one wrapper every
runtime's hub and MCP tools pass through, and from Claude's tool hooks for the
MCP servers Claude calls directly.

```json
{"ts": "2026-11-20T09:14:02.311Z", "kind": "tool", "event": "tool.call",
 "hub": "support", "subject": "priya@example.com", "surface": "web",
 "chat_id": "c_8f2", "client": "203.0.113.7",
 "tool": "read_ticket", "server": "zendesk", "decision": "allow", "outcome": "ok",
 "duration_ms": 412, "result_bytes": 5120, "args": {"ticket_id": "48213", "note": "…"}}
```

- A refused call has `"decision": "deny"` and a `reason`, for example
  `credentials_revoked`, `account_inactive`, `app_tool` or `permission`.
- A call that raised has `"outcome": "error"` and the `error` class.
- A workflow's call has `"subject": "workflow:<name>"` and its `run_id`.

### Sign-in events

`kind: auth`. Never a password, code or key.

| Event | When |
|---|---|
| `auth.signed_in`, `auth.signed_out` | A person signs in or out |
| `auth.sign_in_failed` | A wrong password for a known account |
| `auth.attempt_failed` | Every failed attempt the sign-in limits count: unknown accounts, wrong codes and re-checks too, with the address |
| `auth.locked_out` | The sign-in limit was reached, with `locked_seconds` |
| `auth.api_key_created`, `auth.api_key_revoked` | A personal API key is made or removed |
| `auth.api_key_used` | A key's first use from an address since the process started |
| `auth.app_token_used` | The same for a machine token (a JWT from an API app), with `key` set to `app:<id>` |
| `auth.mfa_*` | Two-step sign-in changes |

### Admin changes

`kind: admin`: grants and removals, blocked accounts, secrets set or deleted
(the name, never the value), workflow run controls and settings changes. Each
has the `actor`, `target`, `target_hub` and a short summary.

### Restricted-tool decisions

`kind: access`. These keep their own table, as before, and are part of the
export. `hubzoid audit <hub>` without `--export` lists them (`--limit`,
`--user`, `--denied`, `--json`).

## Read the Activity page

Console, **Activity** has four tabs:

| Tab | Shows |
|---|---|
| Access changes | Grants, removals and other admin changes |
| Tool decisions | Restricted-tool decisions |
| Tool calls | What `HUBZOID_AUDIT=db` or `both` stored |
| Sign-in events | What `HUBZOID_AUDIT=db` or `both` stored. Organization administrators only. |

Tool calls and sign-in events filter by agent, time, person, allowed or
denied, finished or failed, tool and channel. **Export** downloads what the
current tab holds for the chosen agent and time.

## Export the audit log

An export writes the stored records in a time window, oldest first, one JSON
object per line. The records are the same as the log lines. Use it for a SIEM
backfill or an investigation. Tool calls and sign-in events are stored only
with `HUBZOID_AUDIT=db` or `both`.

From the command line:

```bash
hubzoid audit ./support --export --since 2026-11-01 --until 2026-11-20 --out audit.jsonl
hubzoid audit ./support --export --all-hubs --kind tool,auth
hubzoid audit ./support --export --since 2026-01-01 --export-limit 50000 --out part1.jsonl
```

| Option | Default | What it does |
|---|---|---|
| `--export` | off | Write stored audit records as JSON lines, instead of listing restricted-tool decisions |
| `--since` | none | From this time (ISO 8601, UTC if no zone) |
| `--until` | none | Up to this time |
| `--kind` | `tool,access,admin,auth` | Which records: any of `tool`, `access`, `admin`, `auth` |
| `--all-hubs` | off | Every hub in the deployment's database, not only this one |
| `--out` | standard output | Write to this file |
| `--export-limit` | `1000000` | At most this many records. A last line says where to continue when more remain. |

Over the Console API, with an admin's session or personal API key:

```
GET /portal/api/audit/export?since=2026-11-01T00:00:00Z&kind=tool,access,admin,auth&hub=support
```

The API takes `hub`, `since`, `until`, `kind` and `limit` (default 100,000,
at most 1,000,000). Admin and sign-in events need an organization
administrator. An agent's administrator gets tool calls and restricted-tool
decisions for the agents they manage.

### Continue a long export

When more records remain than the limit allows, the last line is:

```json
{"kind": "export", "event": "export.truncated", "limit": 1000000, "next_since": "..."}
```

Export again with `--since` (or `since=`) set to `next_since`. Records at that
exact instant may repeat.

## Alert on unusual activity

Each signal is a filter on the audit log lines, so it can be a CloudWatch
metric filter or a SIEM rule with no Hubzoid code.

| Signal | Records | Why |
|---|---|---|
| A spike in refused tool calls for one person or one tool | `tool.call` with `decision = deny` | Probing for access, or a prompt injection naming tools |
| A tool call refused because an account was removed or its sign-in revoked | `reason` in `account_inactive`, `credentials_revoked`, `credential_ended` | A session or key that should be dead |
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

For lockouts:

```
{ $.event = "auth.locked_out" }
```

## Tool call spans

OpenTelemetry stays opt-in ([OBSERVABILITY.md](OBSERVABILITY.md)). When a
tracer is set up (run Hubzoid under `opentelemetry-instrument`), every tool
call is also an `execute_tool` span with the GenAI attribute names
(`gen_ai.tool.name`, `gen_ai.tool.call.id`, `error.type`). Spans never carry
arguments or results. Audit records are never sampled.

## Troubleshooting

| Symptom | Check |
|---|---|
| The Tool calls and Sign-in events tabs are empty | `HUBZOID_AUDIT` must be `db` or `both`. Restart after setting it. |
| An export has only access and admin records | The same cause. The command says so on standard error. |
| The API export leaves out admin and sign-in events | They need an organization administrator. |
| Every record shows the proxy's address as `client` | List the proxy in `HUBZOID_TRUSTED_PROXIES` ([DEPLOYING.md](DEPLOYING.md#6-reverse-proxy--tls-optional-recommended)). |
| Standard error reports dropped records | The log driver or database cannot keep up. Check its throughput. |
