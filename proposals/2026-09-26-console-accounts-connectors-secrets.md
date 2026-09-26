# Console accounts, connection journeys and layered secrets

Status: design proposal. The founder approved the plan on 26 September 2026.
This document is the build contract. It authorizes no deployment, and it does not
migrate any existing customer hub.
Base: branch `auth-schedules-upgrade`, release 1.0.1 in progress. Line numbers
refer to the working tree on 26 September 2026. Files being edited in parallel
(`edge.py`, `gateway.py`, `cli.py`, `db.py`) may shift by a few lines.
Open WebUI evidence is from the installed `open-webui==0.11.4`, cited as `OW/...`.

## Problem

Five gaps block the "manager onboards a teammate, teammate uses Gmail from
WhatsApp" story. They also keep Hubzoid tied to Open WebUI's admin screens.

1. **Accounts live in two places.** Today a manager creates the login in Open
   WebUI (Admin Panel, Users). They then grant agent access in the Console
   (`docs/ADMINISTRATION.md:168-172`, `docs/access-management.md:9-12`).
   A hub delegate (per-hub `manage_access`) cannot create an account at all.
   They can grant any capability in their hub, including restricted tools they
   do not hold themselves (`hubzoid/portal.py:446-462`, `:511-530`). R1 recorded
   this as "scope-bounded not holdings-bounded"
   (`proposals/auth-schedules-upgrade.md:54`).
2. **No management API and no agent path.** The authorization rules sit inline
   in the route handlers (`portal.py:430-548`). A chat, WhatsApp or MCP request
   cannot propose an access change. R1 excluded a write API
   (`auth-schedules-upgrade.md:69`).
3. **Connections cannot be started from chat, and they don't reach every runtime.**
   Per-user Open WebUI MCP tokens are injected only by the Claude runtime
   (`factory_claude.py:539-547`). The OpenAI runtime (`runtime.py:150-218`) and
   the Codex runtime (`factory_codex.py:89-104`) connect only static,
   hub-wide servers. The `owui_mcp.py:11-12` docstring wrongly says OpenAI
   injects them too.
   Composio links are raw, unbound, and have no success callback
   (`connections.py:432-447`).
4. **No secrets manager.** All configuration is dotenv files loaded into the
   process environment (`settings.py:270-287`). Nothing reads AWS Secrets Manager
   (no `boto3` or `secretsmanager` in `hubzoid/`).
5. **Configuration layers are implicit.** The deployment layer is "whatever the
   gateway process environment has". A 0.9.6 compatibility path copies some
   keys from hub `.env` files (`gateway.py:307-341`, `cli.py:583-590`).

## Why now

The one live deployment runs its account onboarding by hand: a manager creates
an email/password login and shares it. Delegates are the next ask. The product
brief names MCP and personal assistants as a primary experience, and a
connection that only works in one of three runtimes breaks runtime neutrality
(AGENTS.md). Operators want AWS-held secrets rather than `.env` files on disk.

## Current state (evidence)

### A. Access store and Console

- **One authority exists.** `GrantStore` (`access/store.py:122`) holds direct
  grants on Casbin.
  - Writes are atomic and revision-guarded (`apply_changes`, `:250-285`).
  - Granting a tool implies `use_hub` (`:210-216`).
  - A last-admin guard and an entry cascade protect revokes (`:218-248`).
  - `permissions_for` exists (`:308`). Block/suspend is at `:968` and the
    account-replacement safeguard at `:867`.
- **Every surface uses one decision.** `guard.decide` fails closed and applies
  the surface gate first (`access/guard.py:45-79`). WhatsApp and Telegram are
  outside the default restricted surfaces (`access/policy.py:37`,
  `docs/inbound-surfaces.md:83-85`). Bridge entry requires `use_hub`
  (`server.py:506-560`).
- **Console authentication.** `default_admin_resolver` admits an org or per-hub
  `manage_access` holder (`portal.py:86-128`). The OWUI session is checked
  server-side (`:154-204`). Mutations require the same Origin (`:136-151`).
- **Delegation limits.** Delegates cannot change `manage_access` or remove
  another manager's entry (`portal.py:455-462`, `:517-523`). Account refresh and
  block are org-admin only (`:593-643`). **There is no holdings ceiling.**
- **Audit.** `hz_access_audit(ts, actor, action, subject, hub, permission)`
  (`migrations/operational/versions/0001_baseline.py:49-56`) is written in the
  same transaction as each grant (`store.py:515-531`). It has no surface or
  request column.

What satisfies goals 1 and 5 already:
- the store and `can()`
- atomic apply with a concurrency guard
- the last-admin guard
- block and reactivate
- the verified Console session and CSRF check
- audit
- the directory refresh (`access/owui.py:84-95`)

What is missing:
- account create, password, approve, role and delete
- the delegate ceiling
- a reusable service
- change requests with confirmation
- agent tools
- API-key authentication for the management API

### B. Open WebUI account API (0.11.4)

| Need | Endpoint | Auth | Evidence |
|---|---|---|---|
| Create | `POST /api/v1/auths/add` `{name,email,password,role,profile_image_url}` | admin | `OW/routers/auths.py:1101-1156`. `role` defaults to `pending` (`OW/models/auths.py:80-95`), so always send `role:"user"`. Duplicate email returns 400 `EMAIL_TAKEN` (`auths.py:1112`). The password is validated (`:1117`). |
| Update (password, role, name) | `POST /api/v1/users/{id}/update` | admin | `OW/routers/users.py:915-970`. A password change revokes the user's tokens (`:962-970`). The primary admin is protected (`:924-940`). |
| Delete | `DELETE /api/v1/users/{id}` | admin | `users.py:1042-1081`. Deletes the user's chats (`OW/models/users.py:847-861`). Leaves `oauth_session` and API-key rows. |
| List | `GET /api/v1/users/?page=` | admin | `users.py:82-126`. Already used by `access/owui.py:46-81`. |

- **API keys.** An `sk-` key is accepted on admin endpoints
  (`OW/utils/auth.py:419-436`, `:625-631`). `ENABLE_API_KEYS` defaults off
  (`OW/config.py:2454`).
- **Google sign-in onto a password account.** This needs
  `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`. The flag defaults off
  (`config.py:2490`, merge at `OW/utils/oauth.py:2014-2019`). It does not need
  `ENABLE_OAUTH_SIGNUP`, which is checked only when no account matched
  (`oauth.py:2093-2095`).
  - Use `OAUTH_ALLOWED_DOMAINS` to restrict domains (`config.py:2609`).
  - OAuth settings are env-only by default (`config.py:3232`).
  - Isha's runbook forbids Google SSO while two domains are live
    (`IshaHubAgents/docs/deployment-runbook.md:93-94`).
- **How Hubzoid signs in to Open WebUI.**
  - `access/owui.py:28-43` signs in as `HUBZOID_GATEWAY_ADMIN_EMAIL/_PASSWORD`
    (`gateway_provision.py:115-134`).
  - It uses the internal URL (`deployment.owui_url`, `deployment.py:102-107`).
    `hubzoid run` sets that URL to loopback (`cli.py:324`).
  - The Console service must use this service account. A delegate is never an
    Open WebUI admin and never sees these credentials.

### C. Connectors

| Path | What exists | Gaps |
|---|---|---|
| Open WebUI native MCP (`docs/mcp.md:65`) | `owui_mcp.per_user_specs` reads the caller's Fernet-encrypted `oauth_session` token (`owui_mcp.py:60-107`, `access/owui_oauth_tokens.py`), refreshes it (`owui_refresh.py:86`) and injects an HTTP MCP spec with the caller's bearer. It is switched on by `OWUI_NATIVE_MCP` (`owui_mcp.py:38-42`). | Claude only (`factory_claude.py:539-547`). No surface check (`owui_mcp.py:68`), so a Slack channel mention injects the mentioner's personal token. No link from chat and no success signal. Open WebUI's authorize endpoint checks only that the user is verified, not tool-server access (`OW/main.py:2772-2777`). The callback always redirects to the web UI root (`OW/utils/oauth.py:1319-1326`). A reconnect replaces the previous session (`oauth.py:1287-1290`). |
| Composio (`connections.py:157`) | Sanctioned apps plus a link keyed by normalized email (`connections.py:176-178`, `:432-447`). | The link is not bound to whoever opens it and has no `callback_url`, although the client supports one (`composio_client/resources/link.py:52`). Status is checked only on the next tool call (`:449-455`). A second link can create a duplicate ACTIVE account. |

**WhatsApp identity.** The roster (`identity/access.csv` or `access.py`)
resolves phone to email (`inbound/harness.py:276-302`). Dispatch sends
`X-OpenWebUI-User-Email` (`inbound/dispatch.py:46-50`). So a Claude-backed hub
already injects that user's Open WebUI MCP tokens on WhatsApp. OpenAI and Codex
do not.

**Which path Isha uses: neither, as far as the evidence shows.** I read the
local clone of `IshaHubAgents` (key names only).
- Seen:
  - No `OWUI_NATIVE_MCP`, `COMPOSIO_*`, `CONNECTIONS`, `GMAIL*` or `AWS_*` key
    in any hub `.env` or `restricted/.env`.
  - No connections config.
  - Every `.mcp.json` is empty except ApprovalHub's `playwright` stdio server.
  - The local `webui.db` files have no tool servers and zero `oauth_session`
    rows.
  - The only Composio trace is a throwaway `smoke_connections.py` (GitHub
    toolkit).
  - Only NurturingHub uses WhatsApp. It sets `WHATSAPP_*` and
    `HUBZOID_RESTRICTED_SURFACES`, and its roster is at
    `NurturingHub/nurturing-hub/identity/access.csv`.
  - All hubs use legacy Open WebUI group access.
- Not verified:
  - the production box's `gateway-data/webui.db`
  - the live gateway unit and drop-in
  - the deployed hubzoid version. `requirements.txt:14` installs from git with
    no pin, and the docs disagree.

### D. Settings and secrets

- **Load order.** `settings.load` loads `<hub>/.env` with `override=True`, then
  `restricted/.env` with `override=True`, into `os.environ`
  (`settings.py:270-287`). The bridge is one process per hub, so this is
  hub-wide but process-wide.
- **Gateway isolation.** The gateway restores the environment after each hub's
  load (`gateway.py:198-205`). It inherits allowlisted deployment keys from hub
  `.env` files only when its own environment lacks them (`gateway.py:307-341`).
- **Agent subprocesses.** Codex children get a strict environment allowlist
  (`factory_codex.py:139`). The Claude SDK child inherits all of `os.environ`
  (`claude_agent_sdk/_internal/transport/subprocess_cli.py:815-820`), and
  `factory_claude.py:561` merges `os.environ` into per-turn options. So
  restricted and service credentials reach the `claude` CLI and any stdio MCP
  server it starts.
- **boto3.** 1.42.62 is installed only because open-webui pins it
  (`requirements.lock:98-99`). It is not a declared Hubzoid dependency
  (`pyproject.toml:42-71`).
- **The prs-facade pattern** (`~/Desktop/Isha/IRS/prs-facade/Connectors/aws_secrets.py:7-31`):
  - `load_dotenv()`, then if `AWS_SECRET_NAME` is set, `get_secret_value` in
    `AWS_REGION`
  - `json.loads`, and each key is written to `os.environ`, so **the secret
    overrides `.env`**
  - it passes `AWS_ACCESS_KEY_ID/SECRET` explicitly and prints key names
  - Hubzoid keeps the operator contract (`AWS_SECRET_NAME` + `AWS_REGION`, JSON,
    secret wins over the file). It drops the process-wide injection across
    components and the explicit credentials.

### E. Open WebUI Users page and the edge

- **Existing edge controls.**
  - The edge already blocks browser writes to `/api/v1/groups` behind
    `HUBZOID_LOCK_OWUI_ACCESS_UI` (`edge.py:79-92`).
  - It blocks model ACL writes for managed hubs (`:231-245`).
  - It filters the model picker (`:288-318`) and injects the Console link
    script (`:320-331`, `portal_navigation.py`).
- **Service calls bypass the edge.** Hubzoid's service calls go to Open WebUI
  directly, not through the edge (`edge.py:247-250`).
- **Admin routes.** Open WebUI's admin UI is a single-page app with routes
  `/admin/users` and `/admin/users/[tab]` (tabs `overview`, `groups`).
- **Open WebUI APIs Hubzoid itself needs.** These must stay reachable on the
  internal URL:
  - `auths/signin` and `auths/signup` (`gateway_provision.py:121-131`)
  - groups (`:140-157`)
  - models (`:199-241`, `access/reconcile.py:77-99`, `:165-174`)
  - the users list (`access/owui.py:52`)
  - the session check `GET /api/v1/auths/` (`portal.py:170-174`)
  - the new account endpoints in section B

## Decisions

These are defaults. Only item 1 in Open questions blocks anything.

1. **Hubzoid owns access, Open WebUI owns credentials.** The Console calls Open
   WebUI's admin API with the deployment service account. Hubzoid stores and
   enforces:
   - hub entry
   - restricted-tool, workflow and connector capabilities
   - delegated management

   Open WebUI groups and model ACLs stay a legacy input and a visibility mirror
   only.
2. **One authorization service** (`hubzoid/access/service.py`) serves:
   - the Console
   - `/portal/api` callers using an Open WebUI API key
   - the agent tools on every surface

   No route handler decides authority itself.
3. **Delegate ceiling = the delegate's current effective permissions in that
   hub, minus `manage_access`.** It is checked on every write and again at
   confirmation.
   - Delegates cannot:
     - grant `manage_access`
     - change their own access
     - change public access or org admins
     - make global account changes (password reset, role, delete, block)
   - Org admins keep today's scope. Sub-delegation stays org-admin only.
4. **Accounts are created with role `user` only.** The password is typed or
   generated in the Console. It is shown once for manual sharing and never
   stored, logged, audited or put in a model's context.
5. **Agent tools only propose.** The actor comes from the trusted request
   identity. The change applies only after that same actor confirms the exact
   plan in the Console with a verified web session. The request is single-use,
   short-lived and audited.
6. **Connections are built on Open WebUI native MCP only.** (Amended
   2026-09-26 by founder decision: Composio is being sunset, so this release
   neither introduces nor requires it. The existing optional Composio
   integration in `connections.py` is unchanged and is not part of the
   journey. Composio mentions later in this proposal describe the original
   plan and are superseded.) Each app resolves to exactly one Open WebUI
   server per hub. If two could serve it, the journey refuses and tells
   permitted callers that an administrator must keep one. This is the rule
   against duplicates.
7. **Connectors are restricted-class capabilities.**
   - Each app `<app>` gets a capability `connector_<app>`, checked by the
     existing `guard.decide`. That includes the `HUBZOID_RESTRICTED_SURFACES`
     surface gate, so WhatsApp must be listed there, as for restricted tools.
   - On managed hubs the capability gates both the journey and per-turn token
     injection.
   - On legacy hubs it gates the journey through the legacy group of the same
     name. Existing injection there is unchanged apart from the surface gate.
8. **Secrets are one remote `.env` per layer.** Within a layer, the AWS secret
   overrides the local file, as in prs-facade. Values are read at start. A
   rotation takes effect on restart of that layer's components.
9. **All new behaviour is off by default** for existing deployments. Isha stays
   on legacy access, with the Open WebUI Users page visible, until an explicit
   migration.

## Founder UX review decisions (2026-09-26)

Approved after reviewing the 8dd898e candidate. They refine, and where they
differ supersede, the UI details below. Authorization stays server-side.

1. **Users card.** Counts distinct login accounts in the viewer's scope,
   whatever the period. Service identities and email-only grants are excluded,
   blocked accounts are included, and the subtext reads "Across N hubs". The
   period applies to usage metrics only. It shows "unavailable", never 0, when
   accounts cannot be read.
2. **One "Add user" flow.** A hub's Access page adds either an existing account
   or a new one, together with its initial access. The People page uses the same
   service. A password login is the default. "Google sign-in only" is offered
   only where Google and merge-by-email are configured, and it never asks for or
   shares a password. There are no email-only grants presented as accounts, and
   public sign-up stays closed.
3. **No new service identities in the UI.** Workflows run as ordinary accounts.
   Existing `workflow:*` records stay, labelled "Legacy service identity".
4. **Collapsible capability groups** with "· N selected", and accessible
   toggles and help.
5. **Artifacts.** The general feature is "Artifacts". `share_public_links` is
   labelled "Share artifacts publicly" and grants public links only, never
   publishing. Revoking it ends existing links for good.
6. **Short review and success.** Review lists who, the hub, and what is added
   and removed. Success reads "Access updated".
7. **Icons.** A settings/shield icon for the Admin Console, a spark for agents,
   and a muted role badge.
8. **Open WebUI Admin Panel.** It stays for chat-app administrators, lands on an
   allowed page, and hides the Users section when the Console manages accounts.
   Integrations and Groups stay.
9. **Waiting feedback.** A status line appears from send until the first words.
10. **Demo knowledge** is corrected: three runtimes and the current positioning.
11. **Restricted-tool fixtures** cover the review hubs in tests.

## What should happen

### 1. Accounts and access (goal 1)

**Flow (Isha's current flow, moved into the Console).**
1. A manager opens **People, Add account**.
2. They enter email, display name and a password, typed or **Generate**.
3. They tick initial access in the hubs they manage. Only capabilities within
   their ceiling are selectable. Others show **Outside your access**.
4. They review, then save.
5. Hubzoid checks the plan, then creates the Open WebUI account with
   `POST /api/v1/auths/add` and `role:"user"`.
6. Hubzoid binds the identity (`upsert_identity` with the returned id) and
   applies the grants in one store transaction.
7. It audits `account_create` plus each grant.
8. The page shows the password once with **Copy**. The manager shares it
   manually.

**Failure handling.**
- If the grant write fails after the account was created, Hubzoid deletes the
  just-created account (it has no chats) and reports the error.
- If the email already exists, Hubzoid offers "grant access instead". This is
  the same disclosure as Add person today.
- An email previously bound to a deleted account trips the replacement
  safeguard (`store.py:867`). Only an org admin can re-create and reactivate it,
  in one audited step.

**Org-admin account actions** on person Details:
- **Reset password.** Revokes sessions (`users.py:962-970`).
- **Approve** a pending signup (`pending` to `user`).
- **Chat-app admin role** (`user`/`admin`). Needed once the Users page is
  hidden.
- **Delete account.** Requires typing the email. Also revokes all grants. Keeps
  the last-admin guard.

Changing an account's email is not offered. Create a new account instead,
because grants are keyed on email.

**Google sign-in.** It is a deployment setting, documented in `docs/auth.md`:
- set `GOOGLE_CLIENT_ID/SECRET`
- set `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`
- keep `ENABLE_OAUTH_SIGNUP=false`
- set `OAUTH_ALLOWED_DOMAINS` to the organization's domains

The same Console-created account can then sign in with Google. Doctor warns
when Google is configured without merge. Enabling this for Isha is a separate
operator decision (runbook `:93-94`).

**Hiding the Open WebUI Users page.** Controlled by `HUBZOID_HIDE_OWUI_USERS`,
default off.
- **Enabled after verification.** An operator enables it once Console account
  management is verified on that deployment.
- **Redirects.** The edge 302-redirects hard navigation of `/admin/users` and
  `/admin/users/overview` to `/portal/#/people`. The injected script does the
  same for client-side navigation and hides the Users tab.
- **Groups tab.** `/admin/users/groups` stays while any hub is legacy, because
  legacy access depends on groups.
- **Blocked browser writes.** The edge returns 403 "Manage accounts in the
  Console" for:
  - `POST /api/v1/auths/add`
  - `POST /api/v1/users/{id}/update` (not `/users/user/...`)
  - `DELETE /api/v1/users/{id}`
- **Service calls still work.** Hubzoid's service account calls the internal
  URL and never passes the edge. The account adapter refuses writes when only
  the public `WEBUI_URL` is known.

### 2. Management API, agent tools and confirmation (goal 5)

**Service.**
- `AccessService` owns the rules. Today's inline checks in `portal.py` move into
  it unchanged, and the ceiling and account rules are added.
- Every existing `/portal/api` mutation becomes a thin wrapper, so existing
  tests keep passing.
- The same endpoints accept `Authorization: Bearer sk-...` (Open WebUI API key,
  resolved by `access/owui_api_keys.resolve_email` as MCP does). A bearer caller
  skips the Origin check, since there is no ambient cookie.

This is the management API another frontend can use later. Identity
verification (`access/session.py`) and the account directory
(`access/accounts.py`) are the two adapters a non-Open WebUI frontend would
replace.

**Agent tools.** `hubzoid/tools/access_admin.py`, switched on per hub by
`HUBZOID_MANAGEMENT_TOOLS=true` and effective only on managed hubs.

| Tool | Effect |
|---|---|
| `my_management_scope()` | Read-only. Hubs you manage and what you can grant. |
| `propose_access_change(person, hub, grant=[], revoke=[])` | Validates against the service. Persists a change request. Returns a summary plus the confirmation link. |
| `propose_new_account(email, name, hub, grant=[])` | As above. The password is set by the manager on the confirmation page, never by the model. |

- **The actor.** Always `current_identity()`. No tool has an actor parameter.
- **Surfaces.** The tools refuse the anonymous, `workflow`, `system`, `slack`
  and `slack-channel` surfaces. They run on `owui`, `web`, `api`, `mcp`,
  `whatsapp` and `telegram`, because proposing is harmless and confirmation
  needs a verified web session.
- **Visibility.** The tools are hidden (`is_enabled`) from callers with no
  management scope, and checked again on invoke, because Claude ignores
  `is_enabled`.
- **Registration.** They register through `tools.make_all`, so chat (all three
  runtimes), WhatsApp through the bridge, and hosted MCP get the same rule.

**Confirmation.**
- **Request row.** A change request is a row in `hz_change_requests` holding:
  - a random id
  - the actor and surface
  - the canonical plan JSON and its `plan_hash`
  - a TTL (`HUBZOID_CHANGE_REQUEST_TTL`, default 900 s)
  - a status
- **Link.** `<public>/portal/#/confirm/<id>`. The Console uses hash routing
  (`portal/src/hooks/useRoute.ts`).
- **Confirm page.**
  - It loads the request. Only its actor may see it.
  - It shows the exact diff: person, hub, capabilities and account fields.
  - For account creation, it asks for the password.
  - The page posts back `plan_hash`.
- **On confirm, the server:**
  1. checks the session subject equals the actor
  2. atomically claims `pending` to `applying`, which makes the request
     single-use
  3. re-evaluates the actor's current ceiling
  4. applies the plan through the same service calls
  5. records `confirmed` or `failed`
- **Audit.** `change_proposed`, `change_confirmed`, `change_rejected` and
  `change_expired` rows carry `surface` and `request_id`.
- **Limits.** At most 20 pending requests per actor.
- **Signing in from WhatsApp.** A manager on WhatsApp gets the link and signs in
  on the web. Open WebUI's `/auth?redirect=` is used when the pinned bundle
  honours it for a non-Open WebUI path (verify). Otherwise the page says "sign
  in, then open the link again".

### 3. Connection journey (goal 2)

**Journey:** "connect my Gmail", then a WhatsApp link, then Google, then a
success page, then a WhatsApp confirmation.

1. **The tool call.** The user asks. The agent calls
   `connect_account(app="gmail", reconnect=false)` (`hubzoid/tools/connect_tools.py`).
2. **The tool checks, then records.**
   - It checks, in order:
     1. the `connector_gmail` capability through `guard.decide`, including the
        surface gate
     2. the single provider for the app
     3. the current status with the provider
   - If the account is already connected and `reconnect` is false, it says so
     and stops.
   - Otherwise it writes `hz_connect_states`: a random id, `subject` from the
     trusted identity, surface, `chat_id`, hub, app, provider, provider ref and
     `expires` (`HUBZOID_CONNECT_TTL`, default 600 s).
   - A newer pending journey for the same (subject, app) supersedes the older
     one.
   - It returns `<public>/portal/connect/<id>`. It never returns a provider URL.
3. **The link page.** `GET /portal/connect/<id>`, served by any bridge through
   the existing `/portal` edge route (`cli.py:428`, `:806`).
   - Handles expired and unknown ids.
   - Requires an Open WebUI session.
   - **Binding:** the session email must equal `subject`. Otherwise it returns
     403 "this link was created for another account".
   - Shows "Connect Gmail for <email>".
4. **Start.** `POST /portal/connect/<id>/start` checks the Origin and marks the
   journey `started`.
   - **Open WebUI native MCP:** sets cookie
     `hz_connect=<id>; Path=/; HttpOnly; SameSite=Lax; Max-Age=600`, then
     redirects to `/oauth/clients/mcp:<server_id>/authorize`.
   - **Composio:** calls `link.create(auth_config_id, user_id=subject,
     callback_url=<public>/portal/connect/<id>/done)` and redirects there.
5. **Google consent**, then the provider's callback.
   - **Open WebUI:** the edge sees a 3xx from `/oauth/clients/*/callback`
     carrying `hz_connect`. It rewrites `Location` to `/portal/connect/<id>/done`
     and clears the cookie. This works for both success and error redirects.
6. **Done page.** `GET /portal/connect/<id>/done` verifies status with the
   provider or broker and ignores callback query parameters. The check is:
   - **Open WebUI:** an `oauth_session` row for (user id, `mcp:<server_id>`)
     created or updated after `started`, whose token passes
     `owui_refresh.access_token_for`.
   - **Composio:** an ACTIVE connected account for (subject, toolkit) created
     after `started`. After a verified reconnect, older ACTIVE accounts are
     deleted, so none is duplicated.
   - **Outcomes:** connected, still finishing (the page polls
     `/portal/connect/<id>/status` for up to 30 s), or cancelled/failed ("ask
     again for a new link").
7. **Notification.** The originating hub's inbound server runs a small outbox
   poller. On `connected` it sends the WhatsApp message "Gmail is connected".
   - The poller also finalizes journeys whose user never reached the done page,
     using the same provider check. This is the fallback when the edge rewrite
     is absent.
   - Why a poller: any bridge may serve `/portal`, but only the hub's own
     inbound process holds its WhatsApp credentials.
8. **Continuation (optional, single use).**
   - After a WhatsApp turn whose reply created a journey, the harness attaches
     that turn's verbatim user text to it. The model never supplies it.
   - The confirmation then says "Reply YES within 10 minutes to continue: <text>".
   - A YES from the same sender and chat claims the continuation atomically. The
     text is dispatched as a fresh turn: identity re-resolved, entry and
     capability checks run again.
   - Any other reply clears it.
9. **Other states.**
   - **Expired:** the link stops working.
   - **Started but not finished:** a one-time "not completed" note.
   - **Reconnect:** `reconnect=true`. Open WebUI replaces the session itself.

**Runtime parity.**
- **One per-turn source.** `owui_mcp.per_user_specs` stays the single per-turn
  source. It gains the surface gate and, on managed hubs, the connector
  capability gate.
- **OpenAI.** Per turn, it builds `MCPServerStreamableHttp` servers from the
  specs, connects and closes them inside the request task, and runs a per-turn
  `agent.clone(mcp_servers=[...shared, ...per_user])`.
- **Codex.** Per turn, it connects the servers and adds their function tools to
  a per-turn copy of the registry, never the shared `self.registry`.
- **Rules for all three.**
  - The admin's per-server tool allow-list applies.
  - A per-user tool never shadows a hub tool. On a name collision the server is
    skipped for that turn and logged.
  - Tool naming follows each runtime's existing convention for hub MCP servers.

### 4. AWS secrets (goal 3)

- **Enabling a fetch.** `AWS_SECRET_NAME` plus a region (`AWS_REGION`, or
  boto3's `AWS_DEFAULT_REGION`) enables a JSON fetch at start.
- **Credentials.** They come only from boto3's default chain:
  - an instance, task or pod role on AWS
  - locally, `AWS_PROFILE` or `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, plus
    `AWS_SESSION_TOKEN` for temporary credentials

  Hubzoid never passes keys explicitly.
- **Loading.** `boto3` is imported lazily, only when a secret is named. The
  secret must be a JSON object.
  - Strings are taken as-is. Numbers and booleans are stringified.
  - Null, nested values and `SecretBinary` are refused, naming the key.
  - Keys starting `AWS_`, and the secret-name keys themselves, are refused (no
    credentials or chaining through a secret).
- **Failure is fatal.** It is raised before anything serves. The process exits
  non-zero with the secret name, the layer and the AWS error class (for example
  `AccessDeniedException` or `NoCredentialsError`). Values never appear in
  messages or logs.
- **Logs** show the key count at INFO and key names at DEBUG.
- **Local `.env` files work unchanged.** With no secret name there is no boto3
  import and no network call.
- **IAM.** Grant `secretsmanager:GetSecretValue` (plus `kms:Decrypt` for a
  customer-managed key) on exactly the named ARNs.
  - With one instance role, any process on the host could read every named
    secret. Hubzoid enforces per-hub scoping in how it loads secrets, not in
    IAM.
  - Per-hub IAM isolation needs per-hub task roles, or separate OS users.
- **Agent subprocesses.**
  - The Claude child env gets blank overrides for:
    - `AWS_*` credential and secret-name keys
    - `HUBZOID_GATEWAY_ADMIN_PASSWORD`
    - `WEBUI_SECRET_KEY` and the `OAUTH_*_ENCRYPTION_KEY` keys
    - `DATABASE_URL` and `HUBZOID_OPERATIONAL_DB`
    - `BRIDGE_API_KEYS`
    - every restricted-layer key
  - Restricted tools run in-process, so they are unaffected.
  - Codex already uses an allowlist. OpenAI stdio MCP servers already get the
    MCP SDK's safe default environment.
  - Stated limitation: agents run as the same OS user, so this narrows
    inheritance. It is not a filesystem sandbox.
- **Rotation.** Values are read once at process start. After rotating, restart:
  - deployment secret: the gateway, plus every bridge
  - hub secret: that hub's bridge, inbound and Slack processes
  - restricted secret: that hub's bridge

  Warn: rotating `WEBUI_SECRET_KEY` signs everyone out and makes stored
  `oauth_session` tokens undecryptable, so every connection must be redone.

### 5. Configuration layers (goal 4)

Precedence runs from lowest (1) to highest. A higher row overrides a lower one
for the components it reaches.

| # | Layer | Source | Named AWS secret | Reaches |
|---|---|---|---|---|
| 1 | Built-in | `settings.py` defaults | none | all |
| 2a | Deployment (compat, deprecated) | allowlisted keys from hub `.env` files, only if absent from 2b and 2c (`gateway.py:307-341`, with a warning) | none | gateway and Open WebUI |
| 2b | Deployment | gateway service environment (systemd `EnvironmentFile`, shell). Standalone `hubzoid run`: the hub `.env` plus the process environment. | none | see component scoping |
| 2c | Deployment secret | JSON secret | `AWS_SECRET_NAME` in the gateway environment (standalone: hub `.env` or process env). Recorded by name and region in `deployment.json` so external bridges find it. | see component scoping |
| 3a | Hub | `<hub>/.env` (`override=True`, as today) | none | that hub's bridge, inbound, Slack and agent runtime |
| 3b | Hub secret | JSON secret | `HUBZOID_HUB_SECRET_NAME` read from `<hub>/.env` | same as 3a |
| 4a | Restricted | `<hub>/restricted/.env` | none | that hub's restricted tools (in-process). Blanked for agent subprocesses and Open WebUI. |
| 4b | Restricted secret | JSON secret | `HUBZOID_RESTRICTED_SECRET_NAME` read from `restricted/.env` | same as 4a |

- **Within a layer**, the secret wins over the file. Each overridden key is
  logged by name only.
- **Across layers**, the more specific layer wins.
- **The gateway ignores a hub `.env` naming `AWS_SECRET_NAME`**, with a warning
  pointing to `HUBZOID_HUB_SECRET_NAME`. One hub cannot redirect deployment
  config.

**Component scoping.**
- **Open WebUI subprocess:** the deployment layer minus `HUBZOID_*` and `AWS_*`.
  Never hub or restricted layers. This also stops today's leak of
  `restricted/.env` into Open WebUI under `hubzoid run`.
- **Bridges:** from the deployment layer, only an explicit
  `BRIDGE_DEPLOYMENT_KEYS` allowlist:
  - the `HUBZOID_GATEWAY_ADMIN_*` keys
  - `WEBUI_SECRET_KEY` and the `OAUTH_*_ENCRYPTION_KEY` keys
  - `DATABASE_URL`, `DATABASE_SCHEMA` and `HUBZOID_OPERATIONAL_DB`
  - `HUBZOID_PUBLIC_URL`, `WEBUI_URL` and `OWUI_NATIVE_MCP`
  - `HUBZOID_OTEL_ENDPOINT` and `OTEL_*`

  Ignored keys are named in a warning.
- **External bridges (`--no-bridges`)** fetch the deployment secret themselves
  from the manifest pointer, filtered the same way, before their hub layers.
- **Gateway planning** loads hub settings without fetching secrets
  (`settings.load(..., secrets=False)`). The gateway process never holds a
  hub's secret.

**Deployment-locked keys.**
- `DATABASE_URL`, `DATABASE_SCHEMA` and `HUBZOID_OPERATIONAL_DB` keep today's
  fail-on-conflict behaviour (`ADMINISTRATION.md:56-59`).
- A hub value of `WEBUI_SECRET_KEY` or `OAUTH_*_ENCRYPTION_KEY` that differs
  from the deployment value produces a startup warning, not a failure. This is
  for compatibility: several Isha hub `.env` files carry `WEBUI_*` keys.

`hubzoid doctor` gains a layer report. It lists key names with their layer and
source, never values, plus a fetch check for each named secret.

### 6. Capabilities in the Console (P4)

An administrator manages what a person or group may do from **Agents → the
agent → Access → Edit access**: the existing drawer, the existing store and the
existing service. There is no second permission store and no page per tool
family.

**Two separate questions.**
- **Availability (configuration).** Is the capability implemented, enabled for
  this hub, and are its required settings present? Checked locally, by setting
  name only. "Configured" means present, not verified with the provider.
- **Authorization.** May this person or service use it? Only the grant store
  answers this, through `guard.decide`.
- Adding a credential never grants anyone access. A grant never creates a
  credential or enables a disabled integration. A capability may be granted
  before it is configured; the Console says it cannot run until configured.

**Groups in the drawer** (presentation only; ids and grants do not change):

| Group | Contents today |
|---|---|
| Hub access | Use this agent (`use_hub`) |
| Hubzoid tools | Save shared knowledge (`curator`). Later: `jev` (agent Y), `share_public_links` (agent Z), `connector_<app>` (P2). Only registered, implemented capabilities appear. |
| Custom restricted tools | `restricted/*.py` stems, with optional `identity/permissions.yaml` labels |
| Workflows | Hidden: no workflow-only capability exists yet |
| Administration | Manage access (`manage_access`), which includes delegated account creation |
| No longer available | Granted ids missing from the catalogue, shown only to remove them |

Empty groups are hidden. Rows stay compact: a label, a short status (Required,
Inherited, Included, Outside your access, "Jev key missing", "Not checked") and a
help tooltip for the longer text. Review, confirmation and unsaved-change
handling are unchanged. There is no "Enable all".

**Registration contract** (`hubzoid/capabilities.py`, no plugin framework):

```python
@dataclass(frozen=True)
class Capability:
    permission: str                 # stable id; grants refer to it; never rename
    label: str
    group: str                      # hub | tools | restricted | workflows | admin
    description: str = ""          # help tooltip text
    surfaces: tuple[str, ...] = ()  # implemented surfaces only: chat, mcp, workflow
    requires: tuple[str, ...] = ()  # setting NAMES; checked locally, never values
    missing: str = "Not configured" # short status when a required setting is absent
    enabled_by: str = ""            # optional hub switch; present and false -> disabled
    default: str = "grant"          # grant: explicit, off by default | included: comes with use_hub
    sensitive: bool = False
    delegate_grantable: bool = True # False: only organization admins may grant it
def register(cap: Capability) -> Capability   # at import, by the module that enforces it
def catalog(hub_dir: Path, *, granted=()) -> list[dict]
```

- The enforcing module registers and uses `cap.permission` in its existing
  `guard_tool` call, so the Console row and the backend check share one id.
  `guard_tool` stays the only place that wraps and marks gated tools.
- `catalog` returns the built-ins, P2's `connect_journey.permissions`, the
  restricted discovery (file names only, never imported) and obsolete granted
  ids. `deployment.permission_catalog` becomes a thin wrapper that keeps the
  old fields (`permission`, `label`, `description`, `sensitive`) and adds
  `group`, `surfaces`, `status`, `available`, `default`, `delegate_grantable`
  and `obsolete`.
- Hub metadata in `identity/permissions.yaml` applies to custom restricted
  capabilities only. An entry naming a built-in is ignored with a warning.
- Registration validates: a known group, a lowercase id, and never
  `sensitive` together with `default="included"`.

**Configuration status.** Required names are looked up in
`config_secrets.layer_report(hub_dir, fetch_secrets=False)` (a blank value
counts as absent) and in the environment the process had before its first hub
load. No provider call and no AWS fetch. If a key is absent locally but a named
secret that could supply it was not read, the status is "Not checked", not
missing. Values never leave the check.

**Defaults and boundaries.**
- Every tool capability, including Call Jev, Call LLM and Call agent exposed in
  chat, needs an explicit grant and is off by default. Workflow APIs such as
  `hub.call_jev` are not chat exposure and do not disappear when it is off.
- `included` capabilities have no grant of their own. They are shown checked
  and read-only when the person has Use this agent. The service refuses to
  grant them. A sensitive capability can never be included.
- Delegates grant only within P1's ceiling: what they hold in that hub, minus
  `manage_access`, minus every `delegate_grantable=False` capability. Enabling
  an administrative tool is not organization administration.
- A capability grant never authorizes using another person's connection. The
  artifact Share dialog (agent Z) decides one artifact's audience. The Console
  only grants capabilities such as creating public links; no per-artifact ACLs
  in the drawer.

**Enforcement.** Unauthorized callers do not see a gated tool (`is_enabled`, and
`guard.visible` for Claude and Codex once agent Y lands). A direct call is
refused by the invoke wall. Revocation applies on the next call because the
guard asks the store each time. Identity comes from the trusted request
context, never from model arguments. Granting one capability implies only
`use_hub`.

**Obsolete grants.** A granted id that is no longer in the catalogue stays
visible in its own group, marked "No longer available", and can be removed. It
can never be granted again, and a delegate may remove it only if they hold it
themselves.

**Everyone signed in is no longer granted** (founder request, 26 September).
New hubs need named grants. The Access page's "Public access" switch goes.
- No path creates a new `*` (everyone signed in) `use_hub` grant, for any
  actor, organization administrators included: `AccessService` (so the Console,
  `/portal/api` and the account endpoints), change proposals and their
  confirmation, `hubzoid grant`, and `GrantStore` itself (`grant`,
  `apply_changes`, `grant_many`, `apply_migration`).
- The store accepts one only with `carry_over_public=True`, which only
  migration passes: carrying over demonstrably public legacy access (a public
  Open WebUI model, `--standalone-public`, or a `*` roster row) preserves
  existing access. The plan report says "Everyone signed in (carried over)".
  `hubzoid access rollback` restores a backup's rows as they were.
- An existing grant is never revoked or hidden by this change. It shows as an
  **Everyone signed in** row with Use this agent, not editable, with a
  **Remove** action for organization administrators. The confirmation states how
  many signed-up accounts open the hub only through it. Delegates see it
  read-only. `hubzoid revoke '*' use_hub` still works.
- Other broad paths stay closed: the organization domain carries only
  `manage_access` (organization administrators only, never through proposals);
  the `*` permission is refused; there are no group subjects; a delegate's
  ceiling contains only named capabilities they hold.

**Compatibility.** Ids `use_hub`, `manage_access`, `curator` and every
restricted stem are unchanged. Legacy hubs (group-based, no
`permissions.yaml`) are read-only in the Console as before and behave exactly
as before. Behaviour change for UPGRADING: `permissions.yaml` can no longer
relabel a built-in; nobody can create new access for everyone signed in
(existing grants keep working until an organization administrator removes
them); `hubzoid grant '*'` exits with an error.

### Security model (cross-cutting)

- **Identity sources.** Identity comes only from:
  - a server-verified Open WebUI session
  - an Open WebUI API key (MCP and management API)
  - trusted-front headers behind the bridge key (`server.py:563-623`)

  Model arguments never name an actor.
- **Authorization.** One service, evaluated on every write and again at
  confirmation. Fail closed on store or directory errors.
- **Passwords and secrets.** Passwords travel only in Console request bodies.
  Secrets never enter tool output, audit rows, change requests or logs.
- **Short-lived, single-use state.** Change requests and connect states are
  short-lived, random, single-use, bound to one subject and cleared on expiry.
  They are also checked against the Origin (on POST) or bound to a session (on
  GET).
- **Connector binding and verification.** A connector link is bound to the
  requesting subject and checked against the opener's session. Success is
  verified with the provider, never from callback parameters.
- **Personal tokens need a trusted surface.** Personal tokens are never
  injected on surfaces outside `HUBZOID_RESTRICTED_SURFACES`, which closes the
  Slack-channel gap.
- **Audit.** Every account, access, request and connection outcome is audited.

### Compatibility and the live deployment

- **Isha changes nothing by upgrading.** None of these features can activate
  without new keys:
  - no `AWS_*` key
  - `HUBZOID_MANAGEMENT_TOOLS` and `HUBZOID_HIDE_OWUI_USERS` off
  - no `OWUI_NATIVE_MCP` or `CONNECTIONS`
- **Legacy hubs.** Legacy access stays authoritative. Grants in the Console
  stay read-only for legacy hubs (`portal.py:445`). Console account creation
  works on a legacy deployment, but access for its hubs still comes from Open
  WebUI groups.
- **Behaviour changes to call out in UPGRADING:**
  - The delegate ceiling applies. Managed mode ships in 1.0.x, so no existing
    delegate workflow should depend on the old rule.
  - Personal MCP tokens are no longer injected on Slack, or on WhatsApp and
    Telegram unless those are listed in `HUBZOID_RESTRICTED_SURFACES`.
  - Managed hubs using native MCP need `connector_<app>` grants.
  - Claude subprocesses no longer inherit restricted and service credentials.
- **Rehearsal.** Run this on a clone of IshaHubAgents, including ApprovalHub,
  the one hub with a stdio MCP server. Do it before any production upgrade.
- **Hub structure.** No new required hub files or frontmatter. `connector_`
  becomes a reserved capability prefix, like `use_hub`.

### Total ownership cost

- **P1, the largest.**
  - Mostly new UI plus about 500 lines of Python (service, account adapter,
    tools).
  - It reuses Casbin, the grant store, audit and Open WebUI accounts, with no
    second credential store.
  - New coupling is limited to four stable Open WebUI admin endpoints behind
    one adapter.
- **P2, moderate.**
  - About 600 lines.
  - It deepens the existing coupling to Open WebUI's OAuth internals: the
    `oauth_session` table, the authorize path and the callback redirect. That
    coupling is already a stated reason for the exact `open-webui` pin
    (`pyproject.toml:51`).
- **P3, small.**
  - About 300 lines.
  - boto3 is already installed. No service to run.
- **Rejected alternatives.**
  - A Hubzoid-owned OAuth vault: new crypto and storage.
  - A second account store: splits identity.
  - Composio for everything: moves Isha's tokens to a third party by default.

## Testing plan

The full suite runs serially at each merge:
`../waveAssistEnv/bin/python3 -m pytest`, then the portal build and
`portal/tests/journey.cjs`. Everything below runs without a model, network or
provider credentials unless it is marked `e2e`.

- **Access and accounts (P1).**
  - **Service unit tests:** the ceiling matrix covers org admin, delegate,
    delegate on a public hub, blocked actor, self-change, `manage_access`,
    workflow subjects and revoke symmetry.
  - **Change requests:** expiry, replay, wrong actor, plan-hash mismatch,
    ceiling shrunk before confirm, 20-request cap.
  - **Account adapter** against an httpx mock of the four endpoints:
    - `role:"user"` is always sent
    - `EMAIL_TAKEN` is handled
    - a failed grant deletes the new account
    - it refuses a public-only URL
  - **Password leak check:** assert a password never appears in audit, request
    rows, logs (`caplog`) or responses.
  - **Existing portal tests pass unchanged.**
  - **Edge tests:** Users redirect, write blocks, `/users/user/...` passthrough,
    the callback `Location` rewrite.
  - **Tool tests** for each surface, plus model-argument actor spoofing.
  - **Real Open WebUI walkthrough** at `HubzoidTests/`: create, sign in, reset,
    delete, delegate ceiling, confirm from a chat proposal.
- **Connections (P2).**
  - The journey state machine with a fake provider: bound opener, wrong
    account, expired, cancelled, reconnect, superseded, duplicate guard.
  - Web routes with an Open WebUI session stub.
  - Open WebUI verification against a seeded SQLite `oauth_session` (the
    existing `test_owui_oauth_tokens.py` fixtures).
  - The Composio adapter against a fake client.
  - **Runtime parity** (`tests/test_owui_mcp_parity.py`):
    - an in-process FastMCP HTTP server that answers only its expected bearer
    - for identities X and Y, the per-turn tool surface of Claude (options),
      OpenAI (cloned agent) and Codex (per-turn registry) contains only X's
      server with X's token
    - the shared Codex registry is unchanged afterwards
    - Slack is denied
  - **WhatsApp harness:** tool link, then done, then the poller sends the
    confirmation, then YES dispatches once. A second YES and a non-YES reply do
    nothing.
  - **E2E (marked `e2e`, auto-skip):** one real model turn per runtime calls a
    per-user MCP tool.
- **Secrets and layers (P3).**
  - A stubbed boto3 client via monkeypatch. No moto dependency.
  - Precedence table cases. Value-type validation. Reserved keys.
  - Failure messages contain no values.
  - The gateway ignores a hub `AWS_SECRET_NAME`.
  - Two-hub isolation: A's hub secret keys are absent from B's settings, the
    gateway process and Open WebUI.
  - The Claude child env overrides.
  - Doctor output shows names only.
- **Needs evidence (manual, documented checklists):**
  - real Google consent through a Gmail-capable MCP server
  - a real Composio Gmail link (needs `COMPOSIO_API_KEY`)
  - WhatsApp delivery with a Meta test number
  - an AWS fetch with an instance role and with a local profile
  - Google sign-in merging onto a Console-created account
  - the production Isha box's Hubzoid version and Open WebUI tool-server and
    `oauth_session` counts (read-only, counts only)

## Rollout

1. Land P0. Then build P1, P2 and P3 in parallel worktrees from the P0 commit.
   P1 and P3 start after the in-flight `edge.py`, `gateway.py`, `cli.py` and
   `db.py` change is committed.
2. Merge P3 (inert without keys), then P1, then P2. P2 works without P1's edge
   rewrite because the poller is its fallback.
3. The lead writes the CHANGELOG and UPGRADING entries at merge. Release in the
   next minor.
4. Rehearse the upgrade on a clone of IshaHubAgents with every flag off. Upgrade
   Isha only when the owner asks.
5. Isha adopts these features separately, each by explicit decision:
   - Console accounts (needs an org-admin bootstrap)
   - per-hub migration to managed access
   - `HUBZOID_HIDE_OWUI_USERS`, only after every hub is managed
   - the Gmail journey, once the Gmail backend is chosen (Open questions 1)

## Scope and non-goals

- **No invitations or email delivery.** Self-service password reset and
  account email changes are also out.
- **No Console-managed WhatsApp numbers.** The roster remains the phone-to-email
  source (`hz_identities.phone` exists but stays unused here).
- **No per-restricted-module secret scoping.** The restricted layer is per hub.
- **No other secret backends** (Vault, SSM Parameter Store, GCP) and no hot
  reload.
- **Not built now:** LibreChat or AssistantUI adapters. The service and
  adapters are designed for them.
- **No migration of Isha to managed access.** No Composio default for Gmail. No
  Telegram journey verification. No continuation for web chat.
- **No sub-delegation.** Delegates cannot grant `manage_access`.
- **No Console view of a person's connections.**

## Open questions

1. **Resolved 2026-09-26: (a), Open WebUI native MCP.** Composio is being
   sunset. The original question follows. **Which Gmail backend?** No connector
   path is in use at Isha as far as the evidence shows. Options:
   - **(a)** Open WebUI native MCP with a self-hosted Gmail/Workspace MCP server
     that supports OAuth 2.1, plus a Google OAuth client. An Internal app on
     the Workspace domain avoids Google's restricted-scope verification.
     Personal `gmail.com` users would need a verified External app.
   - **(b)** Composio's hosted Gmail toolkit. A third party holds users' Gmail
     tokens.

   P2 builds the journey and the Open WebUI adapter, and tests with a local
   OAuth MCP server, without this decision.
2. **Production state.** A read-only check of the Isha box is needed before
   claiming which path Isha uses (hubzoid version, tool-server and session
   counts).
3. **Delegate password reset.** Defaulted to org admins only, per "no global
   account changes". It could later be allowed for accounts a delegate created
   that have no access outside the delegate's hubs.
4. **Open WebUI's `/auth?redirect=` for Console paths** needs a check against
   the pinned bundle.

## Definition of done

- **A delegate can onboard within their ceiling.** A delegate creates a normal
  account with a password and grants a subset of their own access. Any wider
  grant is refused server-side (403) on the API, the Console and the tools.
  Everything is audited.
- **Account management moves to the Console.** An org admin can reset, approve,
  change the role of, and delete accounts in the Console.
  - With `HUBZOID_HIDE_OWUI_USERS=true`, `/admin/users` lands on Console People.
  - Browser writes to the account endpoints get 403.
  - Hubzoid's own service calls still succeed.
- **Chat, WhatsApp and MCP proposals need confirmation.** A proposal applies
  only after the same manager confirms the exact plan in the Console. Replay,
  expiry and a shrunk ceiling are refused.
- **The WhatsApp Gmail journey works.**
  - A WhatsApp user asks to connect an app and gets a bound link.
  - Another account opening it gets 403.
  - After consent, the done page reports "connected" from a provider check.
  - WhatsApp confirms, and a YES re-runs the waiting request once.
  - The same user's tool works under Claude, OpenAI and Codex.
  - No duplicate connection exists after a reconnect.
- **Secrets.** With `AWS_SECRET_NAME` and a region, the gateway and bridges
  start with the secret's values, scoped as in the table.
  - A missing permission fails at start with a clear, value-free message.
  - Hub A's secret never appears in hub B, the gateway or Open WebUI.
  - Local `.env` files behave as before.
- **Isha is unaffected.** An IshaHubAgents clone upgrades with no behaviour
  change. `pytest` is green. New modules have unit tests. `e2e` tests auto-skip
  without keys.

## Implementation work packages

**P0 comes first. Then P1, P2 and P3 run in parallel** in separate worktrees
from the P0 commit. Each file below has exactly one owner.
- **Shared files.** The lead writes `CHANGELOG.md` and `docs/UPGRADING.md` at
  merge; both are being edited by others now. `server.py`, `gateway.py` and
  `db.py` are not modified by any package.
- **A package that needs a file it does not own** asks the lead. It does not
  edit the file.

### P0: contracts (lead, sequential, small)

Owns and creates:
- **`hubzoid/migrations/operational/versions/0004_console_connect.py`**
  (`op_0004`, `down_revision="op_0003"`, or the next free revision at P0 time).
  It creates:
  - `hz_change_requests`:
    - columns `id TEXT PK`, `created`, `expires`, `actor`, `surface`, `kind`,
      `hub`, `target`, `plan` (JSON text), `plan_hash`, `base_revision`
      (nullable), `status` (`pending|applying|confirmed|rejected|expired|failed`),
      `decided`, `decided_by`, `result`
    - indexes on (`actor`, `status`) and on `expires`
  - `hz_connect_states`:
    - columns `id TEXT PK`, `created`, `expires`, `hub`, `subject`, `surface`,
      `chat_id`, `handle`, `app`, `provider`, `provider_ref`
    - `status` (`pending|started|connected|cancelled|failed|expired|superseded`)
    - `started`, `finished`, `continuation`
    - `continuation_status` (`none|offered|used|declined|expired`)
    - `notified`
    - indexes on (`hub`, `status`) and on (`subject`, `app`, `status`)
  - nullable columns `surface` and `request_id` on `hz_access_audit`

  Times are `Float(precision=53)`. The migration is forward-only.
- **`hubzoid/access/session.py`:**
  - `verified_email(request, hub_dir) -> str`, moved verbatim from
    `portal._verify_owui_session`
  - `require_same_origin(request) -> None`, moved from
    `portal._check_same_origin`

  `portal.py` imports them and keeps the old names as aliases.
- **Stubs** later owned by P1 and P2:
  - `hubzoid/tools/access_admin.py` (`make(ctx) -> []`)
  - `hubzoid/tools/connect_tools.py` (`make(ctx) -> []`)
  - `hubzoid/connect_journey/__init__.py` (`build_router(hub_dir) -> APIRouter`
    with prefix `/portal/connect` and no routes, `permissions(hub_dir) -> []`)
- **`hubzoid/tools/__init__.py`:** adds `access_admin` and `connect_tools` to
  `make_all`.
- **`hubzoid/portal.py` `mount_portal`:** includes
  `connect_journey.build_router(hub_dir)` before the static `/portal` mount.
- **`hubzoid/config_secrets.py` stub:** `child_env_overrides(env) -> {}`.

Done when the suite is green with no behaviour change.

### P1: authorization service, management API, Console accounts, agent tools (goals 1 and 5)

- **Creates:**
  - `hubzoid/access/service.py`
  - `hubzoid/access/accounts.py`
  - `portal/src/screens/ConfirmScreen.tsx`
  - `portal/src/screens/people/AccountDrawer.tsx`
  - tests `tests/test_access_service.py`, `test_change_requests.py`,
    `test_accounts_owui.py`, `test_portal_accounts.py`,
    `test_tools_access_admin.py`, `test_edge_users_page.py`
- **Owns (modifies):**
  - `hubzoid/portal.py`
  - `hubzoid/access/store.py` (audit kwargs `surface=`, `request_id=`)
  - `hubzoid/tools/access_admin.py`
  - `hubzoid/edge.py`
  - `hubzoid/portal_navigation.py`
  - `portal/src/api.ts`, `portal/src/Portal.tsx`, `portal/src/hooks/useRoute.ts`
  - `portal/src/screens/PeopleScreen.tsx`
  - `portal/src/screens/access/{AccessEditor.tsx,plan.ts,AccessDrawer.tsx}`
  - `portal/tests/journey.cjs`
  - `hubzoid/portal_dist/**` (the build output)
  - `tests/test_portal_api.py`, `tests/test_access_apply.py`,
    `tests/test_edge.py`, `tests/test_edge_lock.py`
  - `docs/access-management.md`, `docs/ADMINISTRATION.md`,
    `docs/PORTAL-ACCOUNT-CONTRACT.md`, `docs/auth.md`

**Contracts provided:**

```python
# hubzoid/access/service.py
@dataclass(frozen=True)
class Actor:
    subject: str   # normalized; from session, API key or bridge identity only
    surface: str   # console|owui|web|api|mcp|whatsapp|telegram
    via: str       # "session"|"api-key"|"bridge"
class Denied(Exception): status: int; code: str
class AccessService:
    def __init__(self, hub_dir: Path): ...
    def catalog(self, hub: str) -> list[dict]            # deployment.permission_catalog + connect_journey.permissions
    def ceiling(self, actor: Actor, hub: str) -> frozenset[str]
    def apply_access_change(self, actor, subject, hub, ops: list[tuple[str, str]],
                            *, expected_revision: int | None = None,
                            request_id: str | None = None) -> int
    def create_account(self, actor, *, email, name, password,
                       grants: list[tuple[str, str]], request_id=None) -> dict
    def set_password(self, actor, subject, password) -> None
    def approve_account(self, actor, subject) -> None
    def set_chat_role(self, actor, subject, role: str) -> None
    def delete_account(self, actor, subject) -> None
    def propose(self, actor, plan: dict) -> dict          # {"id","expires","summary","confirm_path"}
    def confirm(self, actor, request_id, *, plan_hash: str, password: str | None = None) -> dict
    def reject(self, actor, request_id) -> None

# hubzoid/access/accounts.py
class AccountDirectory(Protocol):
    def create(self, *, email, name, password, role="user") -> dict   # {"id","email","name","role"}
    def update(self, account_id, *, password=None, role=None, name=None) -> None
    def delete(self, account_id) -> None
def for_deployment(hub_dir: Path) -> AccountDirectory  # internal URL + service account only
```

**New endpoints** under `/portal/api`:

| Method and path | Notes |
|---|---|
| `POST /accounts` | `{email,name,password,grants:[{hub,permission}]}` |
| `POST /accounts/{subject}/password` | org admin |
| `POST /accounts/{subject}/approve` | org admin |
| `POST /accounts/{subject}/role` | `{role}`, org admin |
| `DELETE /accounts/{subject}` | `{confirm_email}`, org admin |
| `GET /change-requests/{id}` | actor only |
| `POST /change-requests/{id}/confirm` | `{plan_hash,password?}` |
| `POST /change-requests/{id}/reject` | |

`GET /me` gains `grantable: {hub: [permission]}` and `account_admin: bool`.
Every endpoint also accepts `Authorization: Bearer sk-...`.

**Edge contract for P2:**
- **Trigger:** a `GET /oauth/clients/{client_id}/callback` response with status
  3xx, where the request carries cookie `hz_connect` matching
  `^[A-Za-z0-9_-]{20,64}$`.
- **Action:** set `Location: /portal/connect/<id>/done`, and append
  `Set-Cookie: hz_connect=; Max-Age=0; Path=/`.

**Env vars** (documented by P3 in `settings.py`):
- `HUBZOID_MANAGEMENT_TOOLS` (hub, default off)
- `HUBZOID_CHANGE_REQUEST_TTL` (default 900)
- `HUBZOID_HIDE_OWUI_USERS` (deployment and edge, default off)

P1 reads them with `os.environ` and `settings.truthy`.

### P2: connection journey and runtime parity (goal 2)

- **Creates:**
  - `hubzoid/connect_journey/{store.py,providers.py,web.py,notify.py}`
  - tests `tests/test_connect_journey.py`, `test_connect_web.py`,
    `test_owui_mcp_parity.py`, `test_inbound_connect_continuation.py`,
    `tests/e2e/test_connect_e2e.py`
- **Owns (modifies):**
  - `hubzoid/connect_journey/__init__.py`
  - `hubzoid/tools/connect_tools.py`
  - `hubzoid/owui_mcp.py`, `hubzoid/owui_refresh.py`
  - `hubzoid/access/owui_oauth_tokens.py`, `hubzoid/access/owui_tool_servers.py`
  - `hubzoid/connections.py`
  - `hubzoid/runtime.py`, `hubzoid/factory_codex.py`
  - `hubzoid/inbound/harness.py`
  - `tests/test_owui_mcp.py`, `test_owui_mcp_runtime.py`, `test_connections.py`,
    `test_runtime_mcp.py`, `test_codex_runtime.py`, `test_inbound_harness.py`
  - `docs/mcp.md`, `docs/inbound-surfaces.md`

**Contracts provided:**

```python
# hubzoid/connect_journey/__init__.py
def build_router(hub_dir: Path) -> APIRouter   # /portal/connect/{id}, /{id}/start (POST), /{id}/done, /{id}/status
def permissions(hub_dir: Path) -> list[dict]   # [{"permission": "connector_<app>", "label", "description", "sensitive": True}]
def start(hub_dir: Path, *, app: str, reconnect: bool = False) -> dict   # uses current_identity(); {"state": "connected"|"link", "url"?}
def attach_continuation(hub_dir, *, subject, surface, chat_id, since: float, text: str) -> bool
def take_continuation(hub_dir, *, subject, surface, chat_id) -> str | None   # atomic, single use

class Provider(Protocol):          # providers.py: OwuiMcpProvider, ComposioProvider
    name: str
    def status(self, subject: str, app: str) -> str        # connected|none|expired
    def begin(self, journey: dict) -> str                  # browser redirect URL
    def verify(self, journey: dict) -> str                 # connected|pending|failed
    def cleanup_duplicates(self, journey: dict) -> None
```

Order inside P2:
1. The surface gate and connector gate in `owui_mcp`.
2. OpenAI and Codex parity.
3. The journey and the Open WebUI adapter.
4. The WhatsApp poller and continuation.
5. The Composio adapter, last (needed only if Open questions 1 picks Composio).

**Env vars** (documented by P3):
- `HUBZOID_CONNECT_JOURNEY` (hub, default off)
- `HUBZOID_CONNECT_TTL` (default 600)

The surface policy reuses `HUBZOID_RESTRICTED_SURFACES`.

### P3: configuration layers and AWS secrets (goals 3 and 4)

- **Creates:**
  - the real body of `hubzoid/config_secrets.py`
  - tests `tests/test_config_secrets.py`, `test_settings_layers.py`,
    `test_gateway_secrets.py`, `test_claude_child_env.py`
- **Owns (modifies):**
  - `hubzoid/settings.py`, including the docstring entries for every new env
    var from P1 and P2
  - `hubzoid/cli.py` (gateway deployment layer, Open WebUI and bridge env
    scoping, and `plan(..., load=partial(settings.load, secrets=False))`)
  - `hubzoid/deployment.py` (manifest field)
  - `hubzoid/webui.py` (only if Open WebUI env assembly lives there)
  - `hubzoid/factory_claude.py` (child env overrides only)
  - `hubzoid/doctor.py`
  - `pyproject.toml` (`"boto3>=1.42,<2"`, already installed through open-webui)
  - `tests/test_gateway.py`
  - `docs/DEPLOYING.md`

**Contracts provided:**

```python
# hubzoid/config_secrets.py
class SecretFetchError(RuntimeError): name: str; layer: str; reason: str   # never values
def fetch(name: str, *, region: str | None, layer: str) -> dict[str, str]  # boto3 default chain, lazy import
def child_env_overrides(env: Mapping[str, str]) -> dict[str, str]           # {key: ""} for sensitive keys present
BRIDGE_DEPLOYMENT_KEYS: frozenset[str]
# hubzoid/settings.py
def load(hub_dir: Path, *, secrets: bool = True) -> Settings   # default keeps today's behaviour
def layer_report(hub_dir: Path) -> list[dict]                  # [{"key","layer","source"}], names only
```

- **Manifest:** `deployment.json` gains an optional
  `"deployment_secret": {"name": str, "region": str | None}`.
- **Env vars:** `AWS_SECRET_NAME` and `AWS_REGION` (deployment),
  `HUBZOID_HUB_SECRET_NAME` (`<hub>/.env`), `HUBZOID_RESTRICTED_SECRET_NAME`
  (`restricted/.env`). Standard AWS chain variables are never read explicitly.
- **Doctor:** also warns when `GOOGLE_CLIENT_ID` is set without
  `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`.

### P4: capabilities in the Console (organization and registration contract)

- **Creates:** `hubzoid/capabilities.py`, `tests/test_capabilities.py`,
  `tests/test_everyone_access.py`.
- **Owns (modifies):**
  - `hubzoid/deployment.py` (`permission_catalog` becomes a wrapper)
  - `hubzoid/access/service.py` (`catalog`, the ceiling, included and obsolete
    handling)
  - `hubzoid/tools/curator.py` (registers `curator`)
  - `hubzoid/access/migrate.py` (skips included capabilities; carries over and
    reports public legacy access)
  - `hubzoid/access/store.py` (refuses new everyone grants without
    `carry_over_public`), `hubzoid/portal.py` (`public_reliant`, refusal text),
    `hubzoid/cli.py` (`hubzoid grant '*'` refused)
  - `portal/src/screens/access/AccessEditor.tsx` (the switch removed; the
    Everyone row and its Remove)
  - `portal/src/screens/access/{AccessDrawer.tsx,plan.ts}`,
    `portal/src/screens/people/AccountDrawer.tsx`, `portal/src/lib/format.ts`,
    `portal/src/api.ts`, `portal/src/components/common.tsx`,
    `portal/src/portal.css`, `portal/tests/*`, `hubzoid/portal_dist/**`
  - `docs/access-management.md`, `docs/ADMINISTRATION.md`
- **Contract for agents Y and Z:** section 6. The lead wires their
  `register(...)` calls at integration and adds their modules to
  `capabilities.REGISTRANTS`.

### Per-package definition of done

Each package:
- passes its new tests and the full suite, run serially
- updates only its owned files
- documents behaviour changes for the lead's UPGRADING entry
- keeps every new feature inert without its enabling key
