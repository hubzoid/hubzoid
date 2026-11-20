# The Hubzoid API

What another program can call on a Hubzoid agent: a script, a CI job, your own
app's server, another agent framework, or a page that embeds the chat.

Every deployment serves the reference at `/docs` (the schema at
`/openapi.json`, and at `/api/openapi.json` for signed-in people), titled
**Hubzoid API** with the installed version. It lists exactly the published
routes. For each one it shows:

- `x-hubzoid-stability`: `stable` or `beta` (see [Stability](#stability)),
- `x-hubzoid-key-operations`: which API key operations may call it,
- the request and response shapes of the main routes, and the event formats.

The web app's own routes are left out. Use the reference for shapes and this
page for how the pieces fit together.

## Where to send requests

Each agent has its own address under the deployment's public URL:

```text
https://hub.example.com/b/<agent>          # e.g. /b/support-agent
```

The first agent in `hubzoid.yaml` also answers at the root. Ask an agent which
model id to send:

```bash
curl -s https://hub.example.com/b/support-agent/v1/models \
  -H "Authorization: Bearer $HUBZOID_KEY"
```

On a laptop with sign-in off, the agents answer on `http://127.0.0.1:3080`
(or the port `hubzoid run` printed) with no key.

## Credentials

| Credential | Who it acts as | Make it |
|---|---|---|
| **Personal API key** `hzp_...` | One person, within their access and the key's operations | Settings, Account, Security, or `hubzoid auth keys create` |
| **Machine JWT** from an API app | One fixed account, within the app's operations | An administrator registers the app in `hubzoid.yaml` ([below](#machine-callers)) |
| **Embedded app pass** | A person of your app, on the embedded chat routes | [Embedding](embedding.md) |
| **MCP access token** | A person, through their assistant | [MCP server](mcp-server.md) |

### Key operations

| Operation | Can | In preset |
|---|---|---|
| `chat` | Ask agents (`/v1/chat/completions`, `/api/chat`), read and write the person's own conversations, files, shares and notifications | Chat, Everything |
| `read` | Any read (GET) the person can make, including Settings screens they may see | Read only, Everything |
| `manage` | Any other change the person may make, except decisions | Everything |
| `approve` | Decide approvals and questions (`/api/requests/{id}/decisions`) and work-item operations (approve, deny, resolve) the person may decide | None: ask for it |

A key never signs in, changes the password or two-step sign-in, creates or
deletes keys, signs out other devices, or confirms access change requests. A
key limited to some agents can only chat with them. Blocking the person, a
password reset, "revoke all credentials" or deleting the key stops it at once.

```bash
hubzoid auth keys create "Nightly report" --preset read --days 30
hubzoid auth keys create "Approval bot" --operations read,approve --days 30
```

Give `approve` only to a program you trust to decide as you. It is recorded:
the request says `decided_via: api_key` and its settle event names the key.

### Machine callers

A CI job or a cloud workload can call the API with a JWT from its own issuer
instead of a stored key, once an administrator registers it as an **API app**
in `hubzoid.yaml` (or `apps.yaml` in the deployment folder):

```yaml
apps:
  ci:
    mode: jwt
    issuer: https://token.actions.githubusercontent.com
    audience: https://hub.example.com
    jwks_url: https://token.actions.githubusercontent.com/.well-known/jwks
    max_lifetime: 3600
    api:
      account: ci@example.com          # every token acts as this one Hubzoid account
      operations: [chat, read]         # chat, read, manage. Never approve
      require:                         # every token must carry these exact claims
        repository: acme/reports
        ref: refs/heads/main
```

- Make `ci@example.com` an ordinary account with only the grants the job needs.
- `require` is mandatory: an issuer such as GitHub Actions signs tokens for every
  repository, so name what is yours.
- The token is checked like an embedded app's (signature with the app's keys,
  issuer, audience, expiry, `max_lifetime`), then the claims, then the account's
  live access. The key operation rules apply, and a machine never decides.
- Runs it starts record the app (`app:ci`). Removing the app or setting
  `enabled: false` stops its queued and running work.

```bash
curl -s https://hub.example.com/b/reports/v1/chat/completions \
  -H "Authorization: Bearer $ACTIONS_ID_TOKEN" -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Summarise yesterday"}]}'
```

An API app cannot embed the chat, and an embedding app's pass cannot call the
API.

## Ask an agent (OpenAI compatible)

```bash
curl -s https://hub.example.com/b/support-agent/v1/chat/completions \
  -H "Authorization: Bearer $HUBZOID_KEY" -H "Content-Type: application/json" \
  -d '{"model": "support-agent", "messages": [{"role": "user", "content": "Which orders are late?"}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="https://hub.example.com/b/support-agent/v1", api_key=key)
reply = client.chat.completions.create(
    model="support-agent", messages=[{"role": "user", "content": "Which orders are late?"}])
print(reply.choices[0].message.content)
```

With a key the answer is the answer text only: tool activity and thinking are
left out. `stream: true` sends Server-Sent Events with one `id` and `created`
per answer on every chunk, then `[DONE]`. Some chunks carry a status `event`
(for example a tool starting) and no text. OpenAI's clients skip them.

The agent runs with the person's access, tools and knowledge, exactly as in the
chat. A step that needs an approval waits for the person, and the answer says
so.

## Conversations, runs and events

The chat app's own conversations are under `/api/conversations`. They are the
person's own chats, the same ones they see in the web app.

1. `POST /api/conversations` with `{"agent": "<model id>"}` makes one.
2. `POST /api/conversations/{id}/operations` with
   `{"kind": "send", "branch_id": "...", "content": [{"type": "text", "text": "..."}]}`
   and an `Idempotency-Key` header starts a reply and returns its `run_id`.
3. Follow it with `GET /api/chat/runs/{run_id}`, or the conversation's events.

`GET /api/conversations/{id}/events` is a Server-Sent Events stream. Each event
has an SSE `id`, the cursor. Reconnect with `Last-Event-ID` (or `?after=`) and
the stream replays what you missed. An old cursor answers `410 replay_expired`:
reload the conversation. Replay reads the database, so it works on any
replica. Live wake-ups reach another replica on the 15 second keep-alive.

## Attachments

Every caller attaches files the same way: upload each one, then name it in the
message.

1. `POST /api/chat/files` with the file as the multipart field `file` returns
   the stored file (`file_id`, `name`, `mime`, `size`). One request per file,
   up to `HUBZOID_MAX_UPLOAD_BYTES` (25 MiB by default). No conversation is
   needed, so the first message can carry files.
2. Send the message with a file part per file. On `/v1/chat/completions`
   that is OpenAI's file part, `{"type": "file", "file": {"file_id": "..."}}`.
   In the chat API it is `{"type": "file", "file_id": "..."}`.

```bash
id=$(curl -s -H "Authorization: Bearer $KEY" -F file=@orders.csv \
  https://hub.example.com/api/chat/files | jq -r .file_id)
curl -s -H "Authorization: Bearer $KEY" https://hub.example.com/v1/chat/completions \
  -d '{"messages":[{"role":"user","content":[{"type":"text","text":"Total the orders"},
       {"type":"file","file":{"file_id":"'"$id"'"}}]}]}'
```

Images are shown to the model directly (`HUBZOID_VISION`, on by default).
Other files get a short note and the agent reads them with `read_upload`.
Base64 data URLs in a message are refused with a message saying how to upload.
A key can name only files it uploaded.

## Approvals and questions

`GET /api/requests` lists the approvals and questions waiting for the person,
across agents. `GET /api/requests/events?after=N` returns request events after
cursor `N` (polling, not a stream: pass the last `sequence` you saw).

`POST /api/requests/{id}/decisions` decides one, with the `digest` you saw so
a changed request is never decided by mistake:

```bash
curl -s -X POST https://hub.example.com/api/requests/$ID/decisions \
  -H "Authorization: Bearer $APPROVE_KEY" -H "Content-Type: application/json" \
  -d '{"decision": "approve", "digest": "'"$DIGEST"'"}'
```

Only a person signed in, or a key made with `approve`, decides. Any other key,
a machine JWT or an MCP token gets `403 human_action_required`. Hub changes are
decided by a person in the web app only. Work-item operations the person asked
for are under `/portal/api/hubs/{hub}/operations`.

## Workflows

Run a "Run for me" report as yourself:

```bash
curl -s -X POST https://hub.example.com/b/reports/portal/api/my/runs \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: nightly-2026-11-20" \
  -d '{"hub": "reports", "workflow": "program_report", "inputs": {"program": "Advanced"}}'
```

It is the run chat starts: as you, with your access, the workflow's inputs and
surfaces (`api`), and your own copy of its reports. It answers `202` with the
`run_id` and a `result` address (`GET /portal/api/my/runs?hub=&run_id=`) at
once. A workflow that asks for a yes needs `"confirm": true`. The same
`Idempotency-Key` returns the same run (`200`), and with different inputs it is
refused (`409`). `GET /portal/api/my` lists the reports you may run, with
their input schemas. A team run, which acts as the workflow's own account, is
`POST /portal/api/workflows/run` and needs "Run and control workflows".

## Hub changes and personal memory

`POST /api/hub-changes` proposes edits to the hub's files (`knowledge/` and
the rest) with a title and exact edits. A manager's change to `knowledge/` from
a person who is there applies at once. Anything else waits for review in the
web app. `GET/PUT/DELETE /api/memory` is the person's own memory for this
agent.

## Webhooks

A person (or a hub manager, for the hub) can have Hubzoid call a webhook:
`PUT /api/chat/notification-targets/{id}` with an https address, the signing
secret's name (`env:NAME`) and the events, then
`POST .../{id}/verify` and turn it on.

| Event | When |
|---|---|
| `run.completed`, `run.failed`, `run.interrupted` | A reply the person started ends |
| `request.created` | An approval or question waits for someone the target belongs to |

The body is JSON: `type`, `event_id`, `occurred_at`, and `conversation_id`, or
for `request.created` the `request_id`, `kind` and `url` of the Inbox. It never
holds the conversation's or request's content. Check the signature before you
trust it:

```python
import hashlib, hmac, time

def verified(secret: bytes, headers, body: bytes) -> bool:
    ts = headers["x-hubzoid-timestamp"]
    want = "v1=" + hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return abs(time.time() - int(ts)) < 300 and hmac.compare_digest(want, headers["x-hubzoid-signature"])
```

A failed delivery is retried on a schedule. One whose outcome is unknown (a
timeout after sending) is marked `ambiguous` and never retried automatically,
so a receiver never sees it twice by accident.

## Who is calling

`GET /api/auth/session` says who the credential acts as. `GET /api/me/access`
lists the agents and capabilities that person has.

## Stability

- `stable` routes keep their paths, methods, required request fields and
  response fields for at least one minor release after a change is announced
  in the [changelog](../CHANGELOG.md).
- `beta` routes may change in any release. The changelog says when they do.
- `/v1` follows OpenAI's chat completions format.
- A test in the release checks compares the published reference with a saved
  copy, so no change to it ships unnoticed.

## Errors

Errors are JSON with a `detail` (`{"code": "...", "message": "..."}` or a
sentence). `401` means no valid credential, `403` that the person or key may
not do this (`key_not_allowed` names a missing operation), `409` a conflict
with a newer change, `422` a body that is not valid, `503` that the agent is
restarting.
