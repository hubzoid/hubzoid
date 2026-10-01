# Hubzoid web app architecture

How the Hubzoid web app (1.1) is put together: the modes it runs in, the
processes and routes, where its data lives, the HTTP API the browser uses, the
stream format of a reply and the frontend. It is a reference for contributors
and for people who integrate with a deployment. Setup guides are elsewhere:
[quickstart](../quickstart.md), [authentication](../auth.md),
[deployment](../DEPLOYING.md) and [upgrading](../UPGRADING.md).

The web app covers what Hubzoid users relied on Open WebUI for: sign-in
(passwords, Google, Microsoft and standard OpenID Connect), sessions, chat with
streamed tool steps and reasoning, conversation history, uploads, share links,
personal MCP connections, groups and multi-hub gateways. Open WebUI stays
available for this release only, as a legacy mode.

## 1. Modes

| Setting | Values | Default | Read by |
|---|---|---|---|
| `HUBZOID_UI` | `hubzoid`, `openwebui` | `hubzoid` | `hubzoid.appmode.ui_mode()` |
| `HUBZOID_AUTH` (1.0 name `WEBUI_AUTH` still read) | true or false | false | `appmode.auth_enabled()` |
| `HUBZOID_PUBLIC_URL` (1.0 name `WEBUI_URL` still read) | an origin | empty | `appmode.public_url()` |
| `HUBZOID_ALLOWED_ORIGINS` | comma-separated extra origins | empty | `appmode.allowed_origins()` |
| `HUBZOID_SECRET_KEY` | Fernet keys, comma-separated | a key file | `hubzoid.secretbox` |

- **Default mode** (`hubzoid`). The bridge serves the web app, its API under
  `/api/`, the sign-in routes under `/auth` and `/oauth/`, the Console under
  `/portal/` and hosted MCP under `/mcp`. No Open WebUI process runs.
- **Legacy mode** (`openwebui`). Hubzoid 1.0.x behaviour, unchanged: Open
  WebUI provides chat and accounts. None of the web app routes are mounted. It
  needs the `openwebui` extra (`pip install "hubzoid[openwebui]"`).
- **Local mode** is the default mode with sign-in off. Every request that
  names this machine (`localhost`, an IP address, the address the server
  listens on, or a configured origin) is the local owner, `admin@localhost`,
  an administrator. `hubzoid run` and `hubzoid gateway` refuse a network bind
  in local mode unless `HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true`.
- **Upgrade guard.** In the default mode with sign-in on, a hub or gateway
  that has Open WebUI accounts and no Hubzoid accounts stops at start and
  explains the two ways forward: `hubzoid migrate openwebui`, or legacy mode.
  In local mode it prints a notice that old chats can be imported. The local
  owner that local mode creates is not counted as a Hubzoid account, and a
  gateway started in the web app keeps its manifest's record of where Open
  WebUI's database is, so the guard and the migration still find it.

A gateway records the mode, sign-in and allowed origins in its deployment
manifest, so bridges started on their own (`gateway --no-bridges`) behave the
same as the ones it launches. For a hub registered in the manifest, the
recorded mode and the recorded sign-in of a web app deployment win over the
hub `.env` and the bridge's environment, and a bridge whose settings disagree
refuses to start (`appmode.deployment_conflicts`).

## 2. Processes and routing

**One hub** (`hubzoid run`): a bridge on `127.0.0.1:<bridge port>` and an edge
on the public port. The edge sends every path to the bridge, except
`/webhooks/<hub>`, which goes to the hub's inbound process when one runs. It
answers 404 for the bridge's internal API (`/v1`, `/uploads`, `/otel`), refuses
`.` and `..` path segments, drops client-sent `X-Hubzoid-*` and `X-OpenWebUI-*`
headers, never keeps cookies between visitors and streams responses.

**Gateway** (`hubzoid gateway`): one bridge per hub and one edge. Every bridge
uses the same operational store (accounts, sessions, conversations, groups,
access), so any bridge can check a session cookie.

| Path | Goes to |
|---|---|
| `/b/<slug>/api/*`, `/b/<slug>/artifacts/*`, `/b/<slug>/branding/*` | that hub's bridge, with `/b/<slug>` removed and `X-Forwarded-Prefix: /b/<slug>` added |
| `/b/<slug>/mcp` and its two `/.well-known/oauth-*/b/<slug>/mcp...` discovery paths | that hub's bridge, for a hub with `MCP_SERVER=true` |
| `/webhooks/<slug>/*` | that hub's inbound process |
| everything else (`/`, `/auth`, `/oauth/*`, `/api/*`, `/portal/*`, `/s/*`) | the first bridge, with the others as fallbacks |

**Hub-scoped calls.** A call that touches a hub's files or agent goes to that
hub's bridge: `POST /api/chat`, stopping and reading a reply, creating and
deleting a conversation, and uploading and downloading files. The browser
builds these URLs as `${api_base}/api/...`, where `api_base` comes from the
agent list: `""` for a single hub and `/b/<slug>` in a gateway. Deployment-wide
calls (sign-in, the conversation list, shares, connections) use no prefix.

**Identity between Hubzoid processes.** In the default mode the bridge trusts
identity headers only when a signed assertion (`X-Hubzoid-Assertion`, HMAC with
the deployment key, valid for 60 seconds and bound to the hub) covers exactly
their values. The Slack adapter and the inbound process sign their requests.
Any other request to the bridge's OpenAI-compatible API is anonymous.

## 3. Data

Hubzoid's own tables live in the operational store: `<hub>/.hubzoid/hub.db`
for a standalone hub, `<data dir>/hubzoid-operational.db` for a gateway, or
PostgreSQL through `DATABASE_URL` or `HUBZOID_OPERATIONAL_DB`. Migrations
`op_0009` to `op_0012` add the web app tables. They are forward only.

| Table | Holds |
|---|---|
| `hz_users` | accounts. A migrated account keeps its Open WebUI id |
| `hz_user_identities` | external sign-ins: (issuer, subject) to account |
| `hz_sessions` | sessions. Only the SHA-256 digest of each token is stored |
| `hz_auth_links` | one-time set-password and reset links (digests) |
| `hz_auth_attempts` | sign-in rate limits and lockouts |
| `hz_conversations`, `hz_messages`, `hz_shares` | chat history and share snapshots |
| `hz_groups`, `hz_group_members` | groups. A grant names a group as `group:<id>` |
| `hz_connectors`, `hz_connector_tokens`, `hz_connector_flows` | personal MCP connections. Tokens are encrypted with the deployment key |

Existing tables keep their meaning. A block stays in `hz_meta`.
`hz_identities.owui_id` holds the Hubzoid account id (the same value for
migrated people). Files people upload, and files the agent makes in a chat,
live in the hub folder under `.hubzoid/chats/<conversation id>/`.

The deployment key (`HUBZOID_SECRET_KEY`, else `secret.key` next to a gateway's
manifest or in `<hub>/.hubzoid/` for a standalone hub, created with mode 0600
on first use) encrypts connection tokens and connector client secrets, the
short sign-in handshake cookie, and signs identity assertions. It is never
stored in the database. Back it up separately from the data.

## 4. Module map

| Area | Modules |
|---|---|
| Modes | `hubzoid/appmode.py`, `hubzoid/upgrade.py` |
| Accounts, sign-in, sessions | `hubzoid/auth/` (`passwords`, `users`, `sessions`, `links`, `ratelimit`, `oidc`, `routes`, `cli`, `logredact`) |
| Chat backend | `hubzoid/chat/` (`store`, `stream`, `history`, `runs`, `files`, `titles`, `shares`, `routes`), `hubzoid/run_events.py` |
| Personal connections | `hubzoid/connectors/` (`registry`, `discovery`, `oauth_flow`, `tokens`, `per_user`, `routes`) |
| Agents, branding, groups | `hubzoid/webapp_gateway.py`, `hubzoid/groups.py` |
| Identity assertions, channel identity | `hubzoid/assertions.py`, `hubzoid/channel_identity.py` |
| Deployment key | `hubzoid/secretbox.py` |
| Mounting on the bridge | `hubzoid/webapp.py` (before the Console mount) |
| Edge and gateway | `hubzoid/edge.py`, `hubzoid/gateway.py`, the `run` and `gateway` commands in `hubzoid/cli.py` |
| Migration from Open WebUI | `hubzoid/migrate_openwebui.py` |
| Web app and Console | `portal/` (one Vite bundle, built into `hubzoid/portal_dist`) |

## 5. Python interfaces

```python
# hubzoid.auth
AuthUser(id, email, name, role, method)          # role: 'admin' | 'user'
current_user(request, hub_dir) -> AuthUser | None
require_user(request, hub_dir) -> AuthUser        # 401 {"code": "unauthenticated"}
require_admin(request, hub_dir) -> AuthUser       # 403 {"code": "forbidden"}
local_owner(hub_dir) -> AuthUser

# hubzoid.run_events: runtimes yield str | RunEvent from stream_events(prompt)
ToolCall(id, name, args), ToolResult(id, name, ok, message),
ReasoningDelta(text), ReasoningEnd(), Notice(kind, text)  # each has .legacy
text_of(item) -> str        # the 1.0.x text rendering, used by every text consumer

# Runtime protocol
async def stream_events(self, prompt: str) -> AsyncIterator[str | RunEvent]
# stream(prompt) is as_text(stream_events(prompt)): the OpenAI-compatible output is unchanged.

# hubzoid.secretbox
encrypt/decrypt/encrypt_json/decrypt_json(hub_dir, ...); sign/verify(hub_dir, message)

# hubzoid.connectors.per_user.per_user_servers(hub_dir, identity=None, **kw) -> list
```

A chat turn runs as `access.Identity.make(user=<email>, groups=
access.effective_groups(hub_dir, email=<email>, surface="web"), surface="web")`,
so groups, grants and personal connections apply as they do on other surfaces.

## 6. HTTP API

Errors are `{"detail": {"code": "<snake_case>", "message": "<sentence>"}}`.
Every request that changes something and carries the session cookie needs an
`Origin` (or `Referer`) header that matches the request's host or one of the
deployment's origins (`HUBZOID_PUBLIC_URL`, `HUBZOID_ALLOWED_ORIGINS`).
Otherwise the answer is 403 `cross_origin`. Browsers send it.

### 6.1 Sign-in

| Method and path | Body | Result |
|---|---|---|
| `GET /api/auth/session` | | `{authenticated, mode: "local" or "accounts", user?, providers: [{id, name}], password, signup, branding_name}` |
| `POST /api/auth/login` | `{email, password}` | 200 `{user}` and the session cookie. 401 `invalid_credentials`, 403 `pending`, `suspended` or `password_disabled`, 429 `rate_limited` with `retry_after` and `Retry-After`, 409 `sign_in_off` in local mode |
| `POST /api/auth/logout` | | 204. The session ends and the cookie is cleared |
| `POST /api/auth/signup` | `{email, name, password}` | 201 `{status: "pending"}`. 403 `signup_disabled` unless `ENABLE_SIGNUP=true`, 409 `account_exists` |
| `GET /api/auth/link/{token}` | | `{valid, purpose, email}` |
| `POST /api/auth/link/{token}` | `{password}` | sets the password, uses up the link, ends other sessions and signs in: `{user}` and the cookie. 410 `link_invalid`, 422 `invalid_password` |
| `POST /api/auth/password` | `{current_password, new_password}` | 204. The person's other sessions end. 409 `no_password` for an account without one |
| `PATCH /api/auth/me` | `{name}` | `{user}` |
| `GET /oauth/{provider}/login?redirect=` | | 302 to the provider. `provider` is `google`, `microsoft` or `oidc` |
| `GET /oauth/{provider}/callback` | | 302 to `redirect` with the cookie, or to `/auth?error=<code>` |

`user` carries `id`, `email`, `name`, `role`, `method` (how this session signed
in), `password_enabled` and `has_password`. Codes for `/auth?error=` are
`sign_in_off`, `not_configured`, `provider_error`, `access_denied`,
`state_mismatch`, `invalid_token`, `no_email`, `email_not_verified`,
`domain_not_allowed`, `not_linked`, `no_account`, `pending`, `suspended`,
`sign_in_changed` and `unavailable`. `redirect` must be a path on this site
(it starts with `/`, not `//`).

The callback paths are Open WebUI's, so existing Google, Microsoft and OpenID
Connect clients keep working. Configuration and the rules for linking,
sessions and rate limits are in [authentication](../auth.md).

### 6.2 Chat

| Method and path | Scope | Body | Result |
|---|---|---|---|
| `GET /api/conversations?q=&archived=0or1&limit=&cursor=` | deployment | | `{items: [{id, title, title_source, agent, hub, api_base, archived, head_id, created_at, updated_at}], next_cursor}`, newest activity first, 50 per page by default (at most 200) |
| `POST /api/conversations` | hub | `{agent, id?}` | 201 `{conversation}`. 200 when that id is already this person's conversation in this hub |
| `GET /api/conversations/{id}` | deployment | | `{conversation, messages: [{id, parent_id, role, content, status, error, created_at}], head_id}` |
| `PATCH /api/conversations/{id}` | deployment | `{title?, archived?, head_id?}` | `{conversation}` |
| `DELETE /api/conversations/{id}` | hub | | 204. Deletes the messages, the share link and the conversation's files |
| `POST /api/conversations/{id}/files` | hub | multipart `file` | 201 `{file_id, name, size, mime, kind: "image" or "file"}` |
| `GET /api/conversations/{id}/files/{file_id}` | hub | | the file (owner only) |
| `POST /api/chat` | hub | see below | the reply as a stream (6.3) |
| `POST /api/runs/{message_id}/cancel` | hub | | 202 `{status: "cancelling"}` |
| `GET /api/runs/{message_id}` | hub | | `{status, message}`, for a page that reloads while a reply runs |
| `GET`, `POST`, `DELETE /api/conversations/{id}/share` | deployment | | `{share_id, url}`, or 204 for `DELETE` |
| `GET /api/shares/{share_id}` | deployment | | `{title, agent, owner_name, created_at, messages}` for any signed-in person |

`POST /api/chat` body:

```json
{"conversation_id": "c_...", "agent": "<agent id, needed for a new conversation>",
 "parent_id": "m_... or null",
 "message": {"id": "m_...", "content": [{"type": "text", "text": "..."},
             {"type": "file", "file_id": "...", "name": "...", "mime": "...", "size": 123}]},
 "assistant_message_id": "m_..."}
```

- With `message`, the user message is stored under `parent_id` (the latest
  assistant message, or null for the first) and the reply follows it. Without
  `message`, the reply is regenerated under `parent_id`, which must be a user
  message. An edit is a new message with the same parent as the one it edits.
- Ids are chosen by the browser. Message ids match `^[A-Za-z0-9_-]{8,64}$`.
  Conversation ids also start and end with a letter or digit. An id used
  elsewhere is 409 `id_conflict`.
- The reply runs in a server task that outlives the request. It is stored as
  `running` at once, updated while it runs, and finished as `complete`,
  `cancelled` or `error`. Closing the page does not stop it. Cancel does. One
  reply runs per conversation at a time (409 `run_in_progress`).
- The model sees the branch from the first message to the new one, flattened
  as 1.0.x did, with notes for attachments.
- Limits: `HUBZOID_MAX_UPLOAD_BYTES` per file (25 MiB),
  `HUBZOID_MAX_FILES_PER_MESSAGE` (10), 1,000,000 characters of text.
- Titles: the first message, shortened, at once, then one model call (the
  hub's model, or `HUBZOID_TITLE_MODEL`). Never an agent run.
- Usage is recorded with `surface="web"` and `kind="chat"`. Title calls are
  `kind="background"`.
- Download links the agent writes (`/artifacts/<conversation id>/<file>`) open
  with the owner's session, or with the signed link. New signed links last 7
  days (`HUBZOID_ARTIFACT_LINK_TTL`, in seconds, `0` for no expiry).

### 6.3 Stream format

The AI SDK UI message stream, version 1, over server-sent events, with the
header `x-vercel-ai-ui-message-stream: v1`:

```
data: {"type":"start","messageId":"<assistant id>","messageMetadata":{"conversationId":"..."}}
data: {"type":"start-step"}
data: {"type":"data-title","data":{"title":"..."},"transient":true}
data: {"type":"reasoning-start","id":"r1"}
data: {"type":"reasoning-delta","id":"r1","delta":"..."}
data: {"type":"reasoning-end","id":"r1"}
data: {"type":"text-start","id":"t1"}
data: {"type":"text-delta","id":"t1","delta":"..."}
data: {"type":"text-end","id":"t1"}
data: {"type":"tool-input-available","toolCallId":"c1","toolName":"read_knowledge","input":{}}
data: {"type":"tool-output-available","toolCallId":"c1","output":{"status":"ok"}}
data: {"type":"tool-output-error","toolCallId":"c1","errorText":"..."}
data: {"type":"error","errorText":"..."}
data: {"type":"finish-step"}
data: {"type":"finish","messageMetadata":{"status":"complete"}}
data: [DONE]
```

`data-title` can arrive twice: the provisional title, then the generated one.
While the agent is quiet the server sends `: keep-alive` comment lines every 15
seconds. `finish` carries `complete`, `cancelled` or `error`, and an `error`
chunk comes before it on failure. Tool input is shortened (2,000 characters
per string). `SHOW_TOOLS=off` hides tool entries here too.

### 6.4 Message content parts

Stored and returned as:

```json
{"type": "text", "text": "..."}
{"type": "reasoning", "text": "..."}
{"type": "tool-call", "toolCallId": "...", "toolName": "...", "args": {}, "result": {"status": "ok"}}
{"type": "file", "file_id": "...", "name": "...", "mime": "...", "size": 1}
{"type": "image", "file_id": "...", "name": "...", "mime": "...", "size": 1}
```

A reasoning part can have empty text: the agent was thinking and the hub shows
only that (`SHOW_THINKING=indicator`). A failed tool has `result`
`{"status": "error", "message": "..."}`. Only PNG, JPEG, GIF and WebP count as
images.

### 6.5 Agents and branding

`GET /api/agents` returns `{agents: [{id, name, description, suggestions,
avatar_url, hub, api_base}], default_agent}`: the agents the signed-in person
may use (Use this agent on a Console-managed hub). `GET /api/branding` returns
`{name, logo_url, favicon_url, custom_css_url}` for the page chrome, and
`GET /branding/{file}` serves the hub's (or the gateway's) branding folder. See
[branding](../branding.md).

### 6.6 Personal connections

| Method and path | Who | Result |
|---|---|---|
| `GET /portal/api/connectors` | organization administrators | `{connectors}`. Secrets are never returned |
| `POST /portal/api/connectors` | organization administrators | `{id?, name, url, auth_type: "oauth" or "none", client_id?, client_secret?, scopes?, tool_allowlist?, enabled}` |
| `PATCH`, `DELETE /portal/api/connectors/{id}` | organization administrators | |
| `POST /portal/api/connectors/{id}/test` | organization administrators | what discovery found |
| `GET /api/connections` | each person | `[{connector_id, name, connected, status, connected_at, allowed, auth_type, enabled}]` |
| `POST /api/connections/{connector_id}/connect` | each person | `{authorize_url}` |
| `GET /oauth/connectors/{connector_id}/callback` | the browser | 302 to `return_to`, or `/account/connections?connected=<id>`, or the same place with `error=<code>` |
| `DELETE /api/connections/{connector_id}` | each person | 204 |

The capability `connector_<id>` gates use on Console-managed hubs. See
[MCP connectors](../mcp.md#personal-connections-default-ui-mode).

### 6.7 Groups

`GET` and `POST /portal/api/groups`, `GET`, `PATCH` and `DELETE
/portal/api/groups/{id}`, `POST /portal/api/groups/{id}/members` with
`{emails}`, and `DELETE /portal/api/groups/{id}/members/{email}`. Organization
administrators only. A grant may name `group:<id>`, and `can(email, hub,
permission)` is true when the email, one of its groups, or an existing
"Everyone signed in" grant holds it. Groups hold agent access only, never
Manage access.

## 7. Frontend

One Vite bundle (`base: /portal/`). `portal/src/main.tsx` renders the Console
when the path starts with `/portal` and the web app everywhere else. Each is
its own lazily loaded chunk, so web app users never download the Console's
Ant Design code. Web app routes: `/` and `/new` (a new chat), `/c/:id` (a
conversation), `/s/:shareId` (a shared view), `/auth` (sign-in, `?redirect=`,
`?error=`), `/auth/set-password?token=`, `/account` and
`/account/connections`.

- Chat components come from `@assistant-ui/react` (pinned) with
  `@assistant-ui/react-markdown` and Shiki for code. A Hubzoid
  `ChatModelAdapter` posts to `${api_base}/api/chat` and reads the stream in
  6.3. The conversation tree is loaded before the thread mounts, so branches
  survive a reload.
- Tool entries show running, done, failed and stopped states. Reasoning has a
  collapsed panel. Stop, edit and resend, regenerate, a branch picker, copy
  and attachments (picker, drag and drop, paste) are available. "Working on
  it…" shows until the first words arrive.
- The agent picker reads `/api/agents`. `/?agent=<id>` and the Console's
  `/?models=<id>` preselect an agent.
- Light and dark themes, a layout that works at 360 px wide, keyboard access,
  visible focus, labelled controls and a live region for reply status. Strings
  are in `portal/src/app/i18n/en.ts`.
- Administrators see an Admin Console entry in the account menu.

## 8. Tests

```bash
pytest -q -m 'not e2e and not e2e_llm and not e2e_ui and not e2e_browser'
cd portal && npm ci && npm run lint && npm run build && npm run test:app && npm test
```

`MODEL=hubzoid-test/<script>` with `HUBZOID_TEST_RUNTIME=1` runs a scripted,
model-free runtime for tests and demonstrations. Without the flag the model id
is refused. `npm run test:app` drives the web app against an in-memory server
(`portal/tests/app-fixture-server.cjs`). `npm test` drives the Console.
