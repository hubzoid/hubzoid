# The Hubzoid API

The Hubzoid API is what another program can call on a Hubzoid agent: a script,
a CI job, your own app's server or another agent framework. Use it to ask an
agent, follow its runs, decide approvals, start reports and receive webhooks.
To put the chat inside your own app for your users, see [Embedding](embedding.md).

```bash
curl -s https://hub.example.com/b/support-agent/v1/chat/completions \
  -H "Authorization: Bearer $HUBZOID_KEY" -H "Content-Type: application/json" \
  -d '{"model": "support-agent", "messages": [{"role": "user", "content": "Which orders are late?"}]}'
```

Every deployment serves the full reference at `/docs`, with the schema at
`/openapi.json` (and at `/api/openapi.json` for signed-in people). It is titled
**Hubzoid API** with the installed version and lists exactly the published
routes. Each route carries:

| Field | What it says |
|---|---|
| `x-hubzoid-stability` | `stable` or `beta` ([Stability](#stability)) |
| `x-hubzoid-key-operations` | The API key operations that may call it |
| Request and response schemas | On the main routes, plus the event formats |

The web app's own routes are left out. Use the reference for exact shapes and
this page for how the pieces fit together.

## Addresses

Each agent has its own base address under the deployment's public URL:
`https://hub.example.com/b/<agent>`. The first agent in `hubzoid.yaml` also
answers at the root. `GET /b/<agent>/v1/models` names the model id to send.

On a laptop with sign-in off, agents answer on `http://127.0.0.1:3080` (or the
port `hubzoid run` printed) with no credential.

## Credentials

Send every credential as `Authorization: Bearer <credential>`.

| Credential | Acts as | Made by |
|---|---|---|
| Personal API key `hzp_...` | One person, within their live access and the key's operations | That person, in Settings, Account, Security, or with `hubzoid auth keys create` |
| Machine JWT from an API app | One fixed account, within the app's operations | An administrator, in `hubzoid.yaml` ([Call the API from CI](#call-the-api-from-ci)) |
| Embedded app pass | A person of your app, on the AG-UI routes and file upload only | [Embedding](embedding.md) |
| MCP access token | A person, through their assistant | [MCP server](mcp-server.md) |

A request may carry a Hubzoid sign-in cookie or a bearer credential, not both
(`400 ambiguous_credentials`). `GET /api/auth/session` says who a credential
acts as. `GET /api/me/access` lists the agents and capabilities of that person.

### Key operations

| Operation | Allows | Settings choice | CLI preset |
|---|---|---|---|
| `chat` | Ask agents (`/v1/chat/completions`, `/api/chat`), read the chat app, and write the person's own conversations, files, shares, folders, tags, prompts and notifications | Chat with my agents | `chat` (the default) |
| `read` | Any read (GET) the person can make, including Settings screens they may see | Read only | `read` |
| `manage` | Any other change the person may make, except decisions | Everything I can do (with `chat` and `read`) | `all` |
| `approve` | Decide approvals and questions (`POST /api/requests/{id}/decisions`) and work-item operations (`approve`, `deny`, `resolve`) the person may decide | Also decide approvals and questions | In no preset |

Only `approve` reaches the decision routes, whatever else the key holds.
`manage` alone does not.

A key never signs in or out, changes the password or two-step sign-in, creates
or revokes keys, signs out other devices, links identities, or confirms or
rejects access change requests.

| Key setting | Default | Notes |
|---|---|---|
| Expiry | 90 days | 1 to 366 days |
| Agents | All the person's agents | A key limited to some agents may only use `chat` |
| Keys per person | | At most 20 live keys |

The key is shown once. Blocking the person, a password reset, "revoke all
credentials", removing their access or revoking the key stops it at once,
including work it already started.

## Ask an agent

`POST /v1/chat/completions` follows OpenAI's chat completions format, so
OpenAI's clients work:

```python
from openai import OpenAI

client = OpenAI(base_url="https://hub.example.com/b/support-agent/v1", api_key=key)
reply = client.chat.completions.create(
    model="support-agent", messages=[{"role": "user", "content": "Which orders are late?"}])
print(reply.choices[0].message.content)
```

- The agent runs with the person's access, tools and knowledge, as in the chat.
- With a key, the answer holds the answer text only. Tool activity and thinking
  are left out.
- `stream: true` sends Server-Sent Events with one `id` and `created` per answer
  on every chunk, then `[DONE]`. Some chunks carry a status `event` (a tool
  starting, for example) and no text. OpenAI's clients skip them.
- A step that needs an approval waits for the person, and the answer says so.

## Conversations, runs and events

`/api/conversations` holds the person's own chats, the same ones they see in
the web app.

1. `POST /api/conversations` with `{"agent": "<model id>"}` makes one.
2. `POST /api/conversations/{id}/operations` with
   `{"kind": "send", "branch_id": "...", "content": [{"type": "text", "text": "..."}]}`
   and an `Idempotency-Key` header starts a reply. It answers `202` with the `run_id`.
3. Follow the run with `GET /api/chat/runs/{run_id}`, or with the conversation's
   events.

`GET /api/conversations/{id}/events` is a Server-Sent Events stream. Each
event's SSE `id` is a cursor. Reconnect with `Last-Event-ID` (or `?after=`) and
the stream replays what you missed. A cursor that is too old answers
`410 replay_expired`: reload the conversation. Replay reads the database, so it
works on any replica. Live wake-ups reach another replica on the 15 second
keep-alive.

## Attach files

Upload each file, then name it in the message.

1. `POST /api/chat/files` with the file as the multipart field `file`. It answers
   `201` with `file_id`, `name`, `mime` and `size`. One request per file, up to
   `HUBZOID_MAX_UPLOAD_BYTES` (25 MiB by default). No conversation is needed.
2. Add a file part per file. On `/v1/chat/completions` that is
   `{"type": "file", "file": {"file_id": "..."}}`. In a conversation operation
   it is `{"type": "file", "file_id": "..."}`.

```bash
id=$(curl -s -H "Authorization: Bearer $KEY" -F file=@orders.csv \
  https://hub.example.com/api/chat/files | jq -r .file_id)
curl -s -H "Authorization: Bearer $KEY" https://hub.example.com/v1/chat/completions \
  -d '{"messages":[{"role":"user","content":[{"type":"text","text":"Total the orders"},
       {"type":"file","file":{"file_id":"'"$id"'"}}]}]}'
```

Images go to the model directly (`HUBZOID_VISION`, on by default). Other files
get a short note and the agent reads them with `read_upload`. Base64 data URLs
in a message are refused with a message saying how to upload. A key can name
only files it uploaded.

## Decide approvals and questions

`GET /api/requests` lists what waits for the person, across agents.
`GET /api/requests/events?after=N` returns request events after sequence `N`.
It is polling, not a stream: pass the last `sequence` you saw.

`POST /api/requests/{id}/decisions` decides one. Send the `digest` you saw, so
a request that changed since is never decided by mistake:

```bash
curl -s -X POST https://hub.example.com/api/requests/$ID/decisions \
  -H "Authorization: Bearer $APPROVE_KEY" -H "Content-Type: application/json" \
  -d '{"decision": "approve", "digest": "'"$DIGEST"'"}'
```

| Field | Values |
|---|---|
| `decision` | `approve`, `approve_chat`, `approve_always`, `reject`, `request_changes` or `answer` |
| `digest` | The digest you read. The decision fails if it changed |
| `answer` | For a question |
| `note`, `expected_revision` | Optional |

Who may decide: the person in their own browser session, the local owner, or a
personal key that person made with `approve`. The request records
`decided_via: api_key` and its settle event names the key. Any other key, a
machine JWT, an MCP token or an embedded pass gets `403 human_action_required`.
Hub changes are decided by a person in the web app only.

Work-item operations are decided under
`/portal/api/hubs/{hub}/operations/{id}/approve`, `deny` and `resolve`
([Work items](work-items.md)).

### Create a key that can approve

```bash
hubzoid auth keys create "Approval bot" --operations read,approve --days 30
```

In Settings, Account, Security, tick **Also decide approvals and questions**
when you create the key. Give `approve` only to a program you trust to decide
as you.

## Run a report for yourself

`POST /portal/api/my/runs` starts a "Run for me" report as the caller. It is the
same run the chat starts: as you, with your access, the workflow's inputs, and
your own copy of its reports.

```bash
curl -s -X POST https://hub.example.com/b/reports/portal/api/my/runs \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: nightly-2026-11-20" \
  -d '{"hub": "reports", "workflow": "program_report", "inputs": {"program": "Advanced"}}'
```

| Field | Default | What it does |
|---|---|---|
| `hub` | required | The agent the report belongs to |
| `workflow` | required | A workflow the agent offers as a report (`as_tool`) |
| `inputs` | `{}` | Checked against the workflow's input schema |
| `confirm` | `false` | Must be `true` for a workflow declared with `confirm=True` |
| `Idempotency-Key` header | none | 1 to 128 printable characters. The same key returns the same run |

It answers `202` with `run_id`, `status`, `created`, `hub` and `result`, the
address to read the run (`GET /portal/api/my/runs?hub=&run_id=`). A retry with
the same `Idempotency-Key` answers `200` with the same run. Without a key, an
active run with the same inputs is returned the same way.

| Status | Code | Why |
|---|---|---|
| 403 | `surface_not_allowed` | The workflow's `surfaces` leave out `api` |
| 403 | `permission_denied` | The caller may not run this workflow |
| 404 | `not_found` | No such report on this agent |
| 409 | `confirm_required` | Send `"confirm": true` |
| 409 | `idempotency_conflict` | The same key with different inputs |
| 409 | `reports_off`, `on_hold`, `engine_not_running` | Reports are off, a backup is running, or the engine is not running |
| 422 | `validation_failed` | Inputs or the key are not valid |

These errors are `{"detail": "<message>", "code": "<code>"}`.
`GET /portal/api/my` lists the reports you may run, with their input schemas.
A team run, which acts as the workflow's own account, is
`POST /portal/api/workflows/run` and needs "Run and control workflows".

## Call the API from CI

A CI job or a cloud workload can call the API with a JWT from its own issuer
instead of a stored key. An administrator registers it as an **API app**: an
`apps:` entry with an `api:` block in `hubzoid.yaml` (or `apps.yaml` in the
deployment folder, when there is no `hubzoid.yaml`):

```yaml
apps:
  ci:
    mode: jwt
    issuer: https://token.actions.githubusercontent.com
    audience: https://hub.example.com
    jwks_url: https://token.actions.githubusercontent.com/.well-known/jwks
    max_lifetime: 3600
    api:
      account: ci@example.com      # every token acts as this one Hubzoid account
      operations: [chat, read]     # chat, read or manage. Never approve
      require:                     # claims every token must carry
        repository: acme/reports
        ref: refs/heads/main
```

| Setting | Default | What it does |
|---|---|---|
| `mode` | required | `jwt` |
| `issuer`, `audience` | required | The `iss` and `aud` of the tokens. No two apps may share both |
| `jwks_url`, `public_key`, `secret_ref` | required | Exactly one: how the signature is checked |
| `max_lifetime` | `3600` | Longest a token may last, 60 to 86400 seconds |
| `enabled` | `true` | `false` refuses its tokens |
| `api.account` | required | Email of the account every token acts as |
| `api.operations` | required | Some of `chat`, `read`, `manage`. `approve` is refused |
| `api.require` | required | Claim name to an exact value, or a list of allowed values. At least one |

An API app has no `access`, `scopes`, `all_users`, `origins`, `token_to` or
`token_header`. It cannot embed the chat, and an embedding app's pass cannot
call the API.

- Make the account an ordinary one with only the grants the job needs.
- `require` is mandatory because an issuer such as GitHub Actions signs tokens
  for every repository. Name what is yours.
- Each call checks the signature, issuer, audience, expiry and `max_lifetime`,
  then the `require` claims, then the account's live access and the app's
  operations. A machine never decides approvals.
- Runs it starts record the app (`app:ci`). Removing the app, setting
  `enabled: false` or pointing it at another account stops its queued and
  running work.

In GitHub Actions, give the job `permissions: id-token: write` and fetch a
token for the app's audience:

```bash
TOKEN=$(curl -s -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=https://hub.example.com" | jq -r .value)
curl -s https://hub.example.com/b/reports/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Summarise yesterday"}]}'
```

## Propose hub changes and keep memory

`POST /api/hub-changes` proposes exact edits to the hub's files with a title.
A proposal sent with an API key or another bearer token always waits for
review in the web app, and deciding it needs a person's own session. `GET`, `PUT` and `DELETE /api/memory` read and change the person's own
notes for this agent. See [Approvals and hub changes](approvals-and-hub-changes.md).

## Receive webhooks

Hubzoid can call a webhook when a reply ends or when an approval or question
waits for someone.

1. `PUT /api/chat/notification-targets/{id}` with `type: "webhook"`, an https
   `recipient_ref`, the signing secret as `connection_ref: "env:NAME"`, the
   `events`, and `expected_revision: "absent"` for a new target. Add
   `?scope=hub` for a target of the whole agent (people who manage its
   notifications only). The default scope is your own.
2. `POST /api/chat/notification-targets/{id}/verify`. Hubzoid sends a signed
   `hubzoid.verification` call, and a 2xx answer verifies the target.
3. `PUT` it again with `enabled: true`. A target is turned on only once
   verified, and changing its address or secret needs a new verification.

| Event | When | Who it reaches |
|---|---|---|
| `run.completed`, `run.failed`, `run.interrupted` | A reply the person started ends | Their own targets, and the agent's targets |
| `request.created` | An approval or question waits | Own targets of the people who may act on it (the requester and named approvers), and the agent's targets |

`events` defaults to `["run.failed"]`. The deployment's chat notifications must
be on, with the event allowed, in `chat/config.yaml`.

The body is JSON with `type`, `event_id` and `occurred_at`. Run events add
`conversation_id`. `request.created` adds `request_id`, `kind`, `agent`,
`conversation_id` and `url`, the Inbox link. A webhook never holds the
conversation's or the request's content. Check the signature before you trust
the body:

```python
import hashlib, hmac, time

def verified(secret: bytes, headers, body: bytes) -> bool:
    ts = headers["x-hubzoid-timestamp"]
    want = "v1=" + hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return abs(time.time() - int(ts)) < 300 and hmac.compare_digest(want, headers["x-hubzoid-signature"])
```

Delivery:

- A failed delivery is tried up to 5 times, after 1, 5, 30, 120 and 600 seconds.
- A timeout leaves the outcome unknown. The delivery is marked `ambiguous` and
  never retried automatically, so a receiver never sees it twice by accident.
- A target that changed, was turned off or was deleted before sending suppresses
  the delivery.
- `request.created` events are read from the stored request log, so a restart
  loses none from the last 24 hours.
- `GET /api/chat/notification-deliveries` shows a target's history. Email and
  Slack targets are not delivered in this release.

## Stability

- `stable` routes keep their paths, methods, required request fields and
  response fields for at least one minor release after a change is announced
  in the [changelog](../CHANGELOG.md).
- `beta` routes may change in any release. The changelog says when they do.
- `/v1` follows OpenAI's chat completions format.
- A release check compares the published reference with a saved copy, so no
  change to it ships unnoticed.

| Area | Routes | Stability |
|---|---|---|
| OpenAI compatible | `/v1/chat/completions`, `/v1/models` | stable |
| Who is calling | `/api/auth/session`, `/api/me/access` | stable |
| Conversations and runs | `/api/conversations` and its `operations`, `events`, `branches`, `view`, `title`, `/api/chat/runs/{id}`, `/api/chat/models`, `/api/chat/search` | stable |
| | Queue, save, organization, collaborators, feedback, deletion preview, `POST /api/chat`, `/api/runs`, `/api/chat/operations/{id}`, search status | beta |
| Files and artifacts | `/api/chat/files`, conversation files, `/api/chat/artifacts` reads | stable |
| | File references, file reuse, new artifact versions | beta |
| Shares | `/api/shares`, a conversation's share | stable |
| | Previews, export, copies | beta |
| Approvals and questions | `/api/requests` and its events, decisions, cancel | stable |
| Work-item operations | `/portal/api/hubs/{hub}/operations` | beta |
| Hub changes and memory | `/api/hub-changes`, `/api/memory` | beta |
| Workflows | `/portal/api/my`, `/my/runs`, `/my/schedules`, `/portal/api/workflows`, `/workflows/run`, `/portal/api/runs` | beta |
| Webhooks | `/api/chat/notification-targets`, `/api/chat/notification-deliveries` | beta |
| Embedded chat | `/api/agui` and its threads | stable |

Routes not in the reference, such as the work-item routes, may change without
notice.

## Troubleshooting

Errors are JSON with a `detail`: `{"code": "...", "message": "..."}` or a
sentence.

| You see | Why |
|---|---|
| `401` | No valid credential, or it expired or was revoked |
| `403 key_not_allowed` | The key, or the API app, lacks the operation this route needs |
| `403 human_action_required` | A decision without a session or an `approve` key |
| `403 claims_not_allowed` | A machine token whose claims don't match the app's `require` |
| `403 account_unavailable` | The API app's account is missing, blocked or not active |
| `403 unknown_app` | A machine token from an API app sent to the embedded chat routes |
| `400 ambiguous_credentials` | A request carried both a sign-in cookie and a bearer credential |
| `409` | A conflict with a newer change, or a run that is still going |
| `410 replay_expired` | An event cursor that is too old. Reload the conversation |
| `422` | A body that is not valid |
| `503` | The agent is restarting, or a store or key set is unavailable |
