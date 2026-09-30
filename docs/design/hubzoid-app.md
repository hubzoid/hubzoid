# Hubzoid web app: design contract

Status: implementation contract for branch `feat/hubzoid-chat` (1 October 2026).
Source plan: HubzoidAgent `docs/hubzoid-launch-minimum-plan-2026-10-01.html` (revision 2).

The Hubzoid web app replaces Open WebUI for everything Hubzoid users rely on:
sign-in (password, Google, standard OIDC), sessions, chat with streaming tool
calls and reasoning, conversation history, uploads, shares, personal MCP
connections, multi-hub gateways and groups. Open WebUI stays available for one
release as a legacy mode. This file is the contract every implementation lane
builds against. Change it only through the integrator.

## 1. Modes

| Setting | Values | Default | Read by |
|---|---|---|---|
| `HUBZOID_UI` | `hubzoid`, `openwebui` | `hubzoid` | `hubzoid.appmode.ui_mode()` |
| `HUBZOID_AUTH` (fallback `WEBUI_AUTH`) | true or false | false | `appmode.auth_enabled()` |
| `HUBZOID_PUBLIC_URL` (fallback `WEBUI_URL`) | origin | empty | `appmode.public_url()` |
| `HUBZOID_ALLOWED_ORIGINS` | comma list of extra origins | empty | `appmode.allowed_origins()` |
| `HUBZOID_SECRET_KEY` | Fernet keys, comma list | key file | `hubzoid.secretbox` |

* **Default mode** (`hubzoid`): the bridge serves the web app, `/api/*`, `/auth`,
  `/oauth/*`. No Open WebUI process. `server.build_app` calls
  `webapp.mount(app, hub_dir, runtime=, inflight=, settings=, model_label=)`
  before the Console mount.
* **Legacy mode** (`openwebui`): 1.0.x behaviour, byte for byte. None of the new
  routes are mounted. Requires the `openwebui` extra.
* **Local mode** = sign-in off. Every request is the local owner
  (`admin@localhost`, role admin). `hubzoid run` refuses a non-loopback bind
  in local mode unless `HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true`.
* **Upgrade guard**: in default mode with sign-in on, if an Open WebUI database
  exists for this hub or gateway and `hz_users` is empty, startup stops with
  instructions: run `hubzoid migrate openwebui`, or set `HUBZOID_UI=openwebui`.
  In local mode it prints a notice that old chats can be imported.

## 2. Processes and routing

Single hub (`hubzoid run`): bridge on `127.0.0.1:<bridge_port>`, edge on the
public port. In default mode the edge sends every path to the bridge (plus
`/webhooks/<slug>` to the inbound process) and runs none of its Open WebUI
rewrites. It still strips client-sent `X-Hubzoid-*` and `X-OpenWebUI-*` headers.

Gateway (`hubzoid gateway`): N bridges, one edge, no Open WebUI. All bridges
share the operational store (sessions, users, conversations, groups, grants),
so any bridge can validate a session cookie.

| Path | Upstream |
|---|---|
| `/b/<slug>/api/*`, `/b/<slug>/artifacts/*`, `/b/<slug>/mcp`, `/b/<slug>/branding/*` | that hub's bridge, prefix stripped |
| `/webhooks/<slug>/*` | that hub's inbound server |
| everything else (`/`, `/auth`, `/oauth/*`, `/api/*`, `/portal/*`, `/s/*`) | the first bridge |

**Hub-scoped calls.** Anything that touches a hub's files or runtime is sent to
that hub's bridge: `POST /api/chat`, run cancel and status, conversation create
and delete, file upload and download. The client builds the URL as
`${agent.api_base}/api/...`, where `api_base` is `""` for a single hub and
`/b/<slug>` in a gateway. Deployment-wide calls use no prefix.

## 3. Data (operational store)

Migrations `op_0009` to `op_0012` (forward-only). Tables and owners:

| Table | Purpose | Lane |
|---|---|---|
| `hz_users` | people; `id` preserved from Open WebUI on migration | A |
| `hz_user_identities` | external sign-in (issuer, subject) to user | A |
| `hz_sessions` | sessions; token stored as SHA-256 hex | A |
| `hz_auth_links` | one-time set or reset password links (digest) | A |
| `hz_auth_attempts` | sign-in rate limiting and lockout | A |
| `hz_conversations`, `hz_messages`, `hz_shares` | chat history and read-only shares | B |
| `hz_groups`, `hz_group_members` | groups; grant subject `group:<id>` | E |
| `hz_connectors`, `hz_connector_tokens`, `hz_connector_flows` | personal MCP connections | D |

Existing tables keep their meaning. Suspension stays in `hz_meta`
(`store.is_suspended`). `hz_identities.owui_id` holds the Hubzoid user id
(same value for migrated people). A lane that needs a schema change edits its
own migration file above (they are unreleased). Never add a new revision.

## 4. Module map and lane ownership

| Lane | Owns (create or change) | Must not change |
|---|---|---|
| **A** accounts and sign-in | `hubzoid/auth/*`, `access/session.py` (mode dispatch), `access/accounts.py` (`for_deployment` dispatch, new `HubzoidAccounts`), sign-in parts of `access/service.py`, `migrations/.../0009_accounts.py`, Console People screens for invites and resets, `tests/test_auth_*.py`, `tests/oidc_mock.py`, `mcp_oauth_web.py` session switch | other lanes' modules |
| **B** chat backend and events | `hubzoid/run_events.py`, `hubzoid/chat/*`, `hubzoid/testing_runtime.py`, runtime `stream_events` in `runtime.py`, `factory_claude.py`, `factory_codex.py`, `run_once` answer-only fix, `server.py` artifact route session check, `migrations/.../0010_conversations.py`, `tests/test_chat_*.py`, `tests/test_run_events.py` | auth internals, frontend |
| **C** web app frontend | `portal/src/app/*` (chat app), `portal/src/main.tsx`, `portal/package.json`, `portal/index.html`, `portal/vite.config.ts`, theme tokens, `portal/tests/app-*` | Console screens other lanes own, Python |
| **D** personal connections | `hubzoid/connectors/*`, `owui_mcp.per_user_servers` mode dispatch, `connect_journey/*` default-mode path, `migrations/.../0012_connectors.py`, Console Connectors screen, `tests/test_connectors_*.py` | runtimes (only the dispatch call) |
| **E** gateway, groups, assertions | `hubzoid/webapp_gateway.py`, `hubzoid/groups.py`, `hubzoid/assertions.py`, grant resolution for groups in `access/store.py` and `access/resolver.py`, `gateway.py`, the `gateway` command in `cli.py`, edge default-mode behaviour in `edge.py`, Slack and inbound identity, `migrations/.../0011_groups.py`, Console Groups screen and group grantees in the access editor, `tests/test_groups_*.py`, `tests/test_gateway_app*.py` | the `run` command |
| **F** migration from Open WebUI | `hubzoid/migrate_openwebui.py`, `tests/test_migrate_openwebui*.py` | schemas (read them) |
| **G** packaging, run, launch | `pyproject.toml`, the `run` and `init` commands in `cli.py`, `hubzoid/webui.py` start-guard only, `Dockerfile`, `docker/*`, `templates/*` (demo hub), `.github/*`, `.gitignore`, hygiene edits, `doctor.py` mode checks | web app code |

`portal/src/api.ts` and the Console navigation (`Shell.tsx`, `Portal.tsx`) are
shared: append only, keep diffs small. Only lane C adds npm dependencies.
**Nobody commits `hubzoid/portal_dist`**: build locally to test, then
`git checkout -- hubzoid/portal_dist` before committing. The integrator rebuilds it.

## 5. Python interfaces (stable)

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
text_of(item) -> str        # 1.0.x rendering, used by every text consumer

# Runtime protocol gains:
async def stream_events(self, prompt: str) -> AsyncIterator[str | RunEvent]
# stream(prompt) becomes as_text(stream_events(prompt)); output unchanged.

# hubzoid.secretbox
encrypt/decrypt/encrypt_json/decrypt_json(hub_dir, ...); sign/verify(hub_dir, message)

# hubzoid.connectors.per_user.per_user_servers(hub_dir, identity=None, **kw) -> list
# same shape as hubzoid.owui_mcp.per_user_servers
```

Identity inside a chat turn: `access.Identity.make(user=<email>, groups=
access.effective_groups(hub_dir, email=<email>, surface="web", header_groups=None),
surface="web")`, bound with `access.identity_scope(...)` and
`_request_ctx.chat_scope(<conversation id>)`. The per-chat files directory is
keyed by the conversation id.

## 6. HTTP API

Errors: `{"detail": {"code": "<snake_case>", "message": "<sentence for people>"}}`.
Mutations authenticated by cookie require a same-origin `Origin` (or `Referer`)
in `allowed_origins()`, or equal to the request host when none are configured.

### 6.1 Sign-in (lane A)

| Method and path | Body | Result |
|---|---|---|
| `GET /api/auth/session` | | `{authenticated, mode: "local"|"accounts", user?, providers:[{id,name}], password: bool, signup: bool, branding_name}` |
| `POST /api/auth/login` | `{email, password}` | 200 `{user}` and cookie. 401 `invalid_credentials`, 403 `pending` or `suspended`, 429 `rate_limited` with `retry_after` |
| `POST /api/auth/logout` | | 204, cookie cleared, session revoked |
| `POST /api/auth/signup` | `{email, name, password}` | 201 `{status: "pending"|"active"}`. 403 `signup_disabled` unless `ENABLE_SIGNUP` |
| `GET /api/auth/link/{token}` | | `{valid, purpose, email}` |
| `POST /api/auth/link/{token}` | `{password}` | sets the password, uses the link, signs in: `{user}` and cookie |
| `POST /api/auth/password` | `{current_password, new_password}` | 204. Other sessions revoked |
| `PATCH /api/auth/me` | `{name}` | `{user}` |
| `GET /oauth/{provider}/login?redirect=` | | 302 to the provider. `provider` is `google`, `microsoft` or `oidc` |
| `GET /oauth/{provider}/callback` | | 302 to `redirect` with cookie, or `/auth?error=<code>` |

Callback paths match Open WebUI's, so existing Google OAuth clients keep working.
Configuration names match Open WebUI's too: `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`, `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`,
`MICROSOFT_CLIENT_TENANT_ID`, `OPENID_PROVIDER_URL`, `OAUTH_CLIENT_ID`,
`OAUTH_CLIENT_SECRET`, `OAUTH_PROVIDER_NAME`, `OAUTH_SCOPES`,
`OAUTH_ALLOWED_DOMAINS`, `ENABLE_SIGNUP`, `ENABLE_OAUTH_SIGNUP`,
`OAUTH_MERGE_ACCOUNTS_BY_EMAIL`, `WEBUI_ADMIN_EMAIL` and `WEBUI_ADMIN_PASSWORD`
(bootstrap), plus the new `HUBZOID_ADMIN_EMAIL` and `HUBZOID_ADMIN_PASSWORD`.

Rules:
* Cookie `hz_session`: HttpOnly, SameSite=Lax, Path=/, Secure when the request
  scheme (after `X-Forwarded-Proto`) is https. 32 random bytes, stored hashed.
  Absolute lifetime `HUBZOID_SESSION_DAYS` (30), idle `HUBZOID_SESSION_IDLE_DAYS` (7).
* A session is valid only while the user exists, is `active`, is not suspended,
  and the session is not revoked or expired. Password change, role change,
  suspension and deletion revoke sessions.
* External identity links on `(issuer, subject)`. First sign-in links to an
  existing account by email only when the provider says the email is verified
  and `OAUTH_MERGE_ACCOUNTS_BY_EMAIL` is true (Google also checks
  `OAUTH_ALLOWED_DOMAINS`). With `ENABLE_OAUTH_SIGNUP` a new account is created
  `pending` unless the domain is allowed. Never link on an unverified email.
* `redirect` values must be same-origin relative paths (start with `/`, not `//`).
* Rate limits in `hz_auth_attempts`: per IP and per email, 10 failures per 15
  minutes, then locked for 15 minutes. Failures are audited.
* Passwords: argon2 via pwdlib. Verify migrated bcrypt and rehash on success.
  Minimum 8 characters.
* Links: `/auth/set-password?token=` (set) and the same page for resets. 72 hours.
* Console account operations use `HubzoidAccounts` (same `AccountDirectory`
  protocol). "Add user" returns a one-time sign-in link instead of a password.
  "Google sign-in only" creates `password_enabled=0`.

### 6.2 Chat (lane B)

| Method and path | Scope | Body | Result |
|---|---|---|---|
| `GET /api/conversations?q=&archived=0|1&limit=&cursor=` | deployment | | `{items: [{id,title,agent,hub,api_base,archived,created_at,updated_at}], next_cursor}` |
| `POST /api/conversations` | hub | `{agent, id?}` | 201 `{conversation}` |
| `GET /api/conversations/{id}` | deployment | | `{conversation, messages: [{id,parent_id,role,content,status,created_at}], head_id}` |
| `PATCH /api/conversations/{id}` | deployment | `{title?, archived?, head_id?}` | `{conversation}` |
| `DELETE /api/conversations/{id}` | hub | | 204. Deletes messages, shares and per-chat files |
| `POST /api/conversations/{id}/files` | hub | multipart `file` | 201 `{file_id, name, size, mime, kind: "image"|"file"}` |
| `GET /api/conversations/{id}/files/{file_id}` | hub | | the file (owner only) |
| `POST /api/chat` | hub | see below | SSE stream (6.3) |
| `POST /api/runs/{message_id}/cancel` | hub | | 202 |
| `GET /api/runs/{message_id}` | hub | | `{status, message}` for a reloaded page |
| `GET/POST/DELETE /api/conversations/{id}/share` | deployment | | `{share_id, url}` or 204 |
| `GET /api/shares/{share_id}` | deployment | | `{title, agent, owner_name, created_at, messages}` |

`POST /api/chat` body:
```json
{"conversation_id": "c_...", "agent": "<agent id, required when new>",
 "parent_id": "m_... or null",
 "message": {"id": "m_...", "content": [{"type": "text", "text": "..."},
             {"type": "file", "file_id": "...", "name": "...", "mime": "...", "size": 123}]},
 "assistant_message_id": "m_... (optional, client id for the reply)"}
```
* With `message`: insert the user message under `parent_id` (idempotent by id),
  then reply under it. Without `message`: regenerate a reply under `parent_id`,
  which must be a user message.
* Ids are client-generated strings matching `^[A-Za-z0-9_-]{8,64}$`. An id used
  in another conversation is a 409.
* The run executes in a server task that outlives the HTTP response. The
  assistant message is written as `running` at start, updated during the run,
  and finalised `complete`, `cancelled` or `error`. Closing the browser does
  not stop the run. `POST /api/runs/{id}/cancel` does.
* History for the model: the branch from the root to the new user message,
  flattened as 1.0.x did (`[user]` and `[assistant]` blocks, text parts only,
  attachment notes from `server._attachment_note`).
* Limits: `max_upload_bytes` per file (25 MiB default),
  `HUBZOID_MAX_FILES_PER_MESSAGE` (10), images per turn as `vision_inject`.
* Titles: the first user message (truncated) at once, then one direct model
  call (`runtime.complete_once`) for a short title. Never a full agent run.
* Usage is recorded with `surface="web"`, `kind="chat"` (titles `kind="background"`).
* Download links written by the agent: `/artifacts/<conversation id>/<file>`
  accept the signed-in owner's session, or a signed link. New signed links
  expire after 7 days by default.

### 6.3 Stream format

AI SDK UI message stream v1 over SSE, header `x-vercel-ai-ui-message-stream: v1`:

```
data: {"type":"start","messageId":"<assistant id>","messageMetadata":{"conversationId":"..."}}
data: {"type":"start-step"}
data: {"type":"reasoning-start","id":"r1"}
data: {"type":"reasoning-delta","id":"r1","delta":"..."}
data: {"type":"reasoning-end","id":"r1"}
data: {"type":"text-start","id":"t1"}
data: {"type":"text-delta","id":"t1","delta":"..."}
data: {"type":"text-end","id":"t1"}
data: {"type":"tool-input-available","toolCallId":"c1","toolName":"read_knowledge","input":{}}
data: {"type":"tool-output-available","toolCallId":"c1","output":{"status":"ok"}}
data: {"type":"tool-output-error","toolCallId":"c1","errorText":"..."}
data: {"type":"data-title","data":{"title":"..."},"transient":true}
data: {"type":"error","errorText":"..."}
data: {"type":"finish-step"}
data: {"type":"finish","messageMetadata":{"status":"complete"}}
data: [DONE]
```

### 6.4 Message content parts (stored and returned)

```json
{"type": "text", "text": "..."}
{"type": "reasoning", "text": "..."}
{"type": "tool-call", "toolCallId": "...", "toolName": "...", "args": {}, "result": {"status": "ok"}}
{"type": "file", "file_id": "...", "name": "...", "mime": "...", "size": 1}
{"type": "image", "file_id": "...", "name": "...", "mime": "...", "size": 1}
```

### 6.5 Agents and branding (lane E, baseline exists)

`GET /api/agents` returns `{agents: [{id, name, description, suggestions,
avatar_url, hub, api_base}], default_agent}` for the signed-in person, filtered by
`use_hub`. `GET /api/branding` returns `{name, logo_url, favicon_url,
custom_css_url}`. `GET /branding/{file}` serves the hub (or gateway) branding folder.

### 6.6 Personal connections (lane D)

| Method and path | Who | Result |
|---|---|---|
| `GET /portal/api/connectors` | admin | registry, secrets never returned |
| `POST /portal/api/connectors` | admin | `{id?, name, url, auth_type: "oauth"|"none", client_id?, client_secret?, scopes?, tool_allowlist?, enabled}` |
| `PATCH/DELETE /portal/api/connectors/{id}` | admin | |
| `POST /portal/api/connectors/{id}/test` | admin | discovery result |
| `GET /api/connections` | person | `[{connector_id, name, connected, status, connected_at, allowed}]` |
| `POST /api/connections/{connector_id}/connect` | person | `{authorize_url}` |
| `GET /oauth/connectors/{connector_id}/callback` | browser | 302 to `return_to` or `/account/connections?connected=<id>` |
| `DELETE /api/connections/{connector_id}` | person | 204 |

The capability `connector_<id>` still gates use on managed hubs.

### 6.7 Groups (lane E)

`GET/POST /portal/api/groups`, `GET/PATCH/DELETE /portal/api/groups/{id}`,
`POST /portal/api/groups/{id}/members` `{emails}`,
`DELETE /portal/api/groups/{id}/members/{email}`. Organization administrators
only. Grants may name `group:<id>`. `can(email, hub, perm)` is true when the
email, any of its groups, or `*` holds the grant.

## 7. Frontend

One Vite bundle (`base: /portal/`). `main.tsx` renders the Console when the path
starts with `/portal`, otherwise the chat app. Chat app routes (history API):
`/` new chat, `/c/:id` conversation, `/s/:shareId` shared view, `/auth` sign-in
(`?redirect=`, `?error=`), `/auth/set-password?token=`, `/account`,
`/account/connections`.

* Chat components: `@assistant-ui/react` (pinned exact version) with
  `@assistant-ui/react-markdown` and Shiki highlighting. Runtime:
  `useLocalRuntime` with a Hubzoid `ChatModelAdapter` that posts to
  `${api_base}/api/chat` and parses the stream in 6.3, plus a history adapter
  that loads the conversation tree. A Hubzoid-owned sidebar lists conversations.
* Collapsible tool entries with running, done and failed states; reasoning
  panel; stop (calls cancel); edit; regenerate; branch picker; copy; attachments
  (drag, drop, paste, images previewed); suggestions for the chosen agent;
  "Working on it…" while waiting for the first token.
* Agent picker from `/api/agents`; the Console deep link `/?models=<id>` and
  `/?agent=<id>` preselect an agent.
* States: signed out, no agents ("You don't have access to an agent yet…"),
  access denied, network error, run error.
* Tailwind with the Studio tokens (orange accent `#B5471F`, Inter). Light and
  dark themes. Works at 360 px wide. Keyboard reachable, visible focus, labelled
  controls, live region for streaming status. Strings in `src/app/i18n/en.ts`.
* Administrators see a Console link in the sidebar.

## 8. Tests and environment

* Python: `/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python` with
  `PYTHONPATH=<your worktree>`. Run pytest from your worktree root.
  Never install, upgrade or remove packages in that environment.
* Deterministic model for tests: `MODEL=hubzoid-test/scripted` works only when
  `HUBZOID_TEST_RUNTIME=1` (lane B). Scripts cover text, tools, errors,
  reasoning, slow output for cancellation, and artifacts.
* Ports per lane (never 3080 or 8000): A 32xx, B 33xx, C 34xx, D 35xx, E 36xx,
  F 37xx, G 38xx. Stop every server you start.
* Scratch files go in the session scratchpad, never in the repo.
* Regression command before committing:
  `pytest -q -x -m 'not e2e and not e2e_llm and not e2e_ui and not e2e_browser' <your test files> <related existing tests>`.
  The full suite (15 minutes) runs at integration.
* Commit on your lane branch only, small logical commits, ending with the
  co-author line. Never push. Never touch the main checkout at
  `/Users/shreyarao/Desktop/WaveAssist/Hubzoid/HubZoid`.
