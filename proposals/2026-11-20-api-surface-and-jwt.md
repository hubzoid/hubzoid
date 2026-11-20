# The Hubzoid API: published, typed, JWT access tokens and machine callers

Plan, 20 November 2026, on branch `feat/api-surface-jwt` (from `feat/mcp-parity` at f3fd8692, the 2.0.9
release branch). Founder approved the direction the same day after a review of the API and MCP surface
(HubzoidAgent `docs/hubzoid-api-mcp-surface-suggestions-2026-11-20.md`, which holds the inventory and the
comparison with Agno, Mastra, eve and Open WebUI).

## Problem

Hubzoid has about 400 HTTP routes and a personal API key (`hzp_`) can reach most of them, but:

1. `/docs` and `/openapi.json` pick routes by path prefix. They miss `/api/chat` and `/api/runs`, list the
   bridge-only `/v1/files` and retired upload routes, and leave out workflows, schedules, hub changes,
   memory and notification targets. Handlers read raw JSON, so the schema has no request or response bodies.
2. MCP access tokens are opaque (`hz_at_`). Nothing outside Hubzoid can check them, so an AWS AgentCore
   Runtime (which only accepts IAM or a JWT it can verify through an OIDC discovery URL) cannot front a
   hub's MCP server.
3. A key with `manage` can approve, deny or resolve work-item operations (the portal labels every caller
   "session"), while `/api/requests` decisions refuse all keys. The rule is inconsistent.
4. The signed notification webhooks accept `request.created` but nothing produces it, and the delivery
   thread stops after three idle passes, so scheduled retries wait for the next chat event.
5. A script acting as a person cannot start a "Run for me" workflow. Chat and MCP can.
6. Machine callers can only use a person's key. A CI job or a cloud workload cannot sign in with its own
   short-lived identity token.

## What should happen

### J1. JWT access tokens for MCP, on every deployment

- `HubOAuth.issue` mints RFC 9068 access tokens: header `typ: at+jwt`, `alg: RS256`, `kid`. Claims `iss`
  (the authorization server issuer, `<resource>/oauth`, exactly as the metadata says today), `sub` (account
  id), `aud` (the hub's MCP resource URL, the RFC 8707 binding already enforced), `client_id`, `scope`
  (`hub:access`), `iat`, `exp`, `jti`, private `hz_grant`. No email or name. Lifetime unchanged (10 minutes,
  never past the grant).
- Refresh tokens stay opaque, stored hashed, rotated, reuse revokes the grant (unchanged). API keys and
  browser sessions are unchanged.
- `load_access_token`: a token with two dots is verified with `joserfc` against Hubzoid's own key set
  (signature, `typ`, `iss`, `aud`, `exp` with 60 s skew, only RS256, refuse `none`). Then the live grant
  check `valid_grant` runs as today, so a revoked connection or blocked person stops on the next call.
  A legacy `hz_at_` token is still looked up in the store until it expires. New access tokens are no
  longer stored.
- Keys: one RSA key set per deployment in the operational database, table `hz_mcp_signing_keys` (kid =
  JWK thumbprint, private key encrypted with `secretbox`, public JWK, created, superseded, retired). Created
  on first use, so a laptop needs no setup. Signer: the newest key not superseded (ties broken by kid, so
  two bridges that create one at the same moment agree). Keys are re-read every 30 seconds and when a token
  names an unknown `kid`.
- Operator-managed keys: `HUBZOID_MCP_SIGNING_KEY` (a PEM, from the secret store or Secrets Manager) makes
  that key the signer and the database keys are not used. `HUBZOID_MCP_SIGNING_KEYS_PREVIOUS` (PEMs) keep
  verifying. Rotating then means moving the current PEM to the previous list and setting a new one. The
  `kid` is the thumbprint, so it is stable across restarts.
- Rotation of database keys: `hubzoid mcp signing-keys list|rotate [--now]`. A rotation adds a key that
  signs from then on. The previous key keeps verifying and stays in the JWKS for the access lifetime plus
  skew. `--now` stops it verifying in Hubzoid at once (other bridges within 30 seconds). Outside verifiers
  that cached the JWKS may still accept its tokens until their cache expires, and those tokens last at most
  ten minutes. `rotate` refuses when an operator-managed key is set.
- Every accepted JWT must agree with its live grant: `sub` equals the grant's account and `client_id` the
  grant's client. `AccessToken.claims` still carries `email` and `grant_id`, filled from the live grant (not
  the token), because MCP identity and attribution read them (`mcp_server.py:165`).
- Revocation (`/mcp/oauth/revoke` and the connections page) finds the grant from a verified JWT, keeping the
  SDK's client binding.
- Discovery: the existing RFC 8414 document `/.well-known/oauth-authorization-server<path>` gains `jwks_uri`
  and is the primary discovery URL (AgentCore's runtime guide accepts it). The public keys are served at
  `<issuer>/jwks`. A minimal `<issuer>/.well-known/openid-configuration` with the same fields is also served,
  because AWS's API and CloudFormation references name only that pattern. Hubzoid issues no ID tokens.
- Proof includes an outside verifier that knows only the discovery URL (PyJWT `PyJWKClient`) checking a
  real token, which is what AgentCore does.
- Not in scope: running on AgentCore. The open AWS questions (an invocation URL that does not end in
  `/mcp`, protected resource discovery on a Runtime, IAM and JWT on one Runtime, header forwarding) are
  recorded in the MCP docs as untested.

### A0. An opt-in `approve` key operation

- `api_keys.OPERATIONS` gains `approve`. It is in no preset (`chat`, `read`, `all` are unchanged).
- Decision routes need `approve` and nothing else grants them: `POST /api/requests/{id}/decisions`,
  `POST /portal/api/hubs/{hub}/operations/{id}/approve|deny|resolve`. This closes the gap where `manage`
  could decide work-item operations. Access change requests (`/change-requests/.../confirm|reject`) stay
  never-with-a-key. Only personal keys can hold `approve`: machine credentials (J2) never decide.
- One classification of credentials: `AuthUser.automated` (an API key or a machine JWT). `human_only`,
  `person_present` and the HITL origin use it, so a key or machine call is never treated as a person in the
  browser.
- `hitl.routes.human_only` allows a personal key that holds `approve` and still refuses every other bearer.
  The decision is stored with `decided_via="api_key"` and the key id goes into the settle event.
- Work items: the portal keeps the credential kind (`api-key` in `Actor.via`) and the decision services
  record it with the key id on the saved decision and in the audit row.
- `hubzoid auth keys create --operations chat,read,approve` and `POST /api/me/api-keys` accept explicit
  operations. The Settings key form gets an "Also approve requests" option and shows keys with extra
  operations correctly. The web app bundle is rebuilt by the release consolidation.

### A1, A2, A3. The published API

- **Selection (A2).** `openapi_doc.py` replaces prefixes with an explicit table of (method, path)
  entries, each with a stability (`stable` or `beta`) and the key operation it needs. Published:
  the current public set (minus `/v1/files`, the retired chunk-upload routes, `/api/chat/files/doctor`,
  `/api/chat/search/rebuild`), plus `POST /api/chat`, `/api/runs/...`, `/api/chat/runs/{id}`,
  `/api/hub-changes...`, `/api/memory`, `/api/chat/notification-targets...`,
  `/api/chat/notification-deliveries`, the personal schedule routes `/portal/api/my...`, the team run route
  `POST /portal/api/workflows/run` and `GET /portal/api/workflows`, `GET /portal/api/runs`, the work-item
  operation decision routes, and the new A4 routes. Each operation gets `x-hubzoid-stability` and
  `x-hubzoid-key-operation`. `/api/admin`, `/api/parity`, `/internal`, auth and MFA stay out. Usage waits
  for the usage page work.
- **Shapes (A1).** Request and response schemas are added as documentation only: Pydantic models in a new
  `hubzoid/api_schemas.py`, attached through `openapi_extra` (request bodies) and the schema builder
  (responses). Handlers keep `read_json`, so limits, empty bodies, aliases and error payloads do not change.
  Event contracts are documented as named schemas: conversation SSE (`Last-Event-ID` or `after`, `410
  replay_expired`, single-replica wake-up note), AG-UI events, and the `/api/requests/events` polling cursor.
- **Stability (A3).** `docs/api.md` gets a stability section: `stable` routes keep their paths, methods,
  required fields and response fields for at least one minor release after a deprecation is announced in the
  changelog. `/v1` follows OpenAI. A snapshot test (`tests/test_openapi_snapshot.py` against
  `tests/fixtures/openapi-public.json`) fails on a removed path or method, a changed type or enum, a new
  required request field, a removed response field, or a changed key operation or stability, and prints the
  command to refresh it (`python -m hubzoid.openapi_doc --write tests/fixtures/openapi-public.json`).
  The same tests check that every published (method, path) resolves to a real route, and post the
  documented example bodies of the main routes to the real handlers.
- The key operations shown per route are computed from `api_keys.allows` itself
  (`x-hubzoid-key-operations: [...]`, every operation that may call it), not maintained by hand. The
  existing "no `/portal` routes" test changes to "only the listed `/portal` routes".

### A4. Run a "Run for me" workflow over the API

Built on the existing personal Settings routes, so listing and reading need nothing new:

- `GET /portal/api/my` already lists the reports a person may run, with input schemas.
- `POST /portal/api/my/runs` (new): body `{"hub", "workflow", "inputs", "confirm"}`. Runs as the caller
  through one start helper shared with `report_tools` (access, the workflow's surfaces with surface `api`,
  input validation, backup hold, envelope with `trigger: "api"`, `scope: "personal"`). A workflow with
  `confirm: true` needs `"confirm": true`. Returns `202` with the run id and the address to read it.
  When the engine runs in another process, the run is queued through its system database with the same
  queue and per-person partition.
- Idempotency: an `Idempotency-Key` (1 to 128 printable characters) makes the run id deterministic
  (`<personal prefix>k<sha256(key)[:16]>`), so DBOS admits it once. The same key returns the same run
  (`200`). The same key with different inputs is `409`. Without a key, an active run with the same inputs is
  returned, as in chat.
- `GET /portal/api/my/runs?hub=&run_id=` reads it through the personal history reader
  (`workflows/observe.py`), whose filter now admits `trigger: "api"`, so API runs also appear in chat's
  `my_reports` and the ready email.
- Key operations: `manage` to start, `read` to list and read. Team runs stay on
  `POST /portal/api/workflows/run`, now published.

### A6. Documentation and the notification webhooks

- One documentation pass: `docs/api.md` rewritten around the published reference (credentials, key
  operations table including `approve`, machine callers, stability, events, webhooks, errors), and
  `docs/mcp-server.md` and `docs/mcp-oauth-claude.md` for J1. The website's endpoints page is updated in its
  own repository to link to `/docs` for shapes.
- Webhook fixes, reusing the persisted HITL events instead of a second event source: the deliverer reads
  new `request.created` events from the HITL event log after a stored cursor (so a crash between the request
  and the notice loses nothing), works out who may act on each request with the HITL service, and creates
  one delivery per subscribed webhook target of those people (unique per target and event). The payload
  carries `request_id`, `kind`, `title`, the agent and the Inbox link, never the request's content. No
  inbox item is made, since the Inbox already lists requests. The deliverer sleeps until the next due
  delivery or new event (capped at 30 seconds) instead of exiting after three idle passes, and starts at
  boot.

### A8. MCP `my_requests`

A read-only MCP tool on the parity registry: the caller's waiting approvals and questions (title, kind,
agent, age, Inbox link), from the same `hitl.service.list_for` the Inbox uses. It never decides.

### J2. Machine callers with their own JWT

- An entry in `apps:` (`hubzoid.yaml` or `apps.yaml`) may be an **API app** instead of an embedding app:

  ```yaml
  apps:
    ci:
      mode: jwt
      issuer: https://token.actions.githubusercontent.com
      audience: https://hub.example.com
      jwks_url: https://token.actions.githubusercontent.com/.well-known/jwks
      max_lifetime: 3600
      api:
        account: ci@example.com              # every token acts as this one Hubzoid account
        operations: [chat, read]             # chat, read, manage. Never approve
        require:                             # required, exact matches on signed claims
          repository: acme/reports
          ref: refs/heads/main
  ```

- One fixed account per app, never a mapping from an email claim (signed email claims may be unverified),
  and `require` must name at least one claim, because shared issuers such as GitHub Actions sign tokens for
  every repository. An API app has no `access`, `scopes`, `origins` or `token_to`, and cannot embed the chat.
- The embedded-app verifier is split into signature and claim checks (shared) and the embedding policy
  (unchanged). The API policy checks `require`, then resolves the configured account (active, not blocked).
- Ingress: the edge admits a bearer JWT whose issuer and audience pick an API app on the public API routes,
  as it admits `hzp_` keys today. The bridge's `/v1` check, the origin exemption for mutations and the file
  upload dispatch treat it as a personal credential, not as an embedding pass.
- The resulting `AuthUser` has `credential="app_jwt"`, `credential_id="app:<id>"` and the app's operations,
  checked by the same `api_keys.allows` rules (and its never-list). No agent restriction (a key limited to
  some agents is chat-only, and API apps are not).
- Runs started this way record the credential (`app_jwt`, `app:<id>`), and `credential_live` checks it on
  every tool call: the app still exists, is enabled, is an API app and still maps to the same account. So
  removing or disabling the app stops queued and running work, as revoking a key does. The token's own
  expiry is checked at admission, like a session's.

## Why this shape

Everything reuses existing pieces: `joserfc` (already used for embedded apps), the OAuth store and
`secretbox`, the embedded-app verifier and registry, the key operation rules, the report tool start path,
FastAPI's schema builder. No new service, no new identity provider, no new token type for people.
Agno issues JWT MCP access tokens by default, Agno and Mastra accept JWTs from your own identity
provider, and everyone keeps long-lived keys opaque. Hubzoid follows the same split.

## Non-goals

Running Hubzoid on AgentCore (needs the AWS test), A2A (later requirement), a knowledge API, a typed client,
the OpenAI Responses API, ID tokens, publishing `/api/admin`.

## Risks

- Signing keys are a new trust root. They live encrypted in the operational database or in the secret store,
  are included in backups like the rest of that database, and can be rotated. Losing them only forces clients
  to sign in again.
- A0 removes `manage`'s ability to decide work-item operations. No known caller uses it. The changelog says so.
- J2 lets an outside issuer act as an account. It is opt-in per app, bounded by the account's live access and
  the listed operations, and an `account_claim` mapping only matches existing accounts.
- Publishing routes creates a compatibility promise. A3's snapshot test and stability tags manage it.

## Definition of done

Focused tests per item. One Codex review of the implementation. End to end on a copy of HubzoidTestHub:
MCP sign-in with a JWT access token, the token checked by an outside verifier from the discovery URL, a
revoked connection refused on the next call, a key rotation, an `approve` key deciding a request while a
`manage` key cannot, `/docs` showing the new routes with bodies, a "Run for me" run started and read over the
API, a `request.created` webhook delivered and verified, `my_requests` over MCP, and a JWT from a test issuer
calling the API. Proof page in `HubzoidTestHub/api-surface-e2e/proof/proof.html`. CHANGELOG under 2.0.9.

## Review

Codex review of this plan, 20 November 2026. All 16 findings were checked against the code. Applied:
J2 maps a fixed account per app with required claims, never an email claim (1), drops agent limits (2),
carries the credential into running work and `credential_live` (3), adds edge, bridge, origin and upload
handling (7, 8) and its own parser and policy (9). Machines never approve and one `automated` flag replaces
per-site credential checks (10). JWT revocation (4) and the internal identity contract (5) are kept
explicitly. Operator-managed key rotation and the outside-cache caveat for `--now` (6). A4 reads runs through
the personal history reader and defines idempotency with deterministic run ids (11, 12). Decision audits
carry the credential (13). `request.created` reads the persisted HITL events with a cursor (14). Snapshot
tests also resolve routes and post example bodies, and key operations are computed (16). Partly applied:
(15) the RFC 8414 document is the primary discovery URL, and the small OpenID-style document stays because
AWS's own API reference names only that pattern.

## Implementation review

Codex review of the implementation (gpt-6.1-sol, high), 20 November 2026, on the branch rebased onto the
2.0.9 release head. All 11 findings were checked against the code and fixed, with tests: API-started
workflow runs now carry the key or machine credential, remapping an API app ends work started as the old
account, idempotent run ids are scoped to the hub and a concurrent conflict gets 409, the remote-engine path
deduplicates active runs, the request.created cursor and payload failures recover, the deliverer stops with
the app, malformed JWT headers give 401, the edge admits only real JWT-shaped bearers, and work-item audits
name the deciding key. Before the review, the e2e had already found one more: an unknown JWT bearer was the
local owner with sign-in off. Fixed. The release gets one fresh review of the whole 2.0.9 line before it ships.
