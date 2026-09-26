# Workflow identity, published reports and owner email

Status: implementation plan. The founder approved the scope on 26 September 2026
(the "Agent Z" brief). This document is the build contract for branch
`workflow-identity-artifacts`, based on `843da41` (Console accounts P0).
It authorizes no deployment, no merge and no change to any customer hub.

Concurrent work this plan must not compete with:
- Agent X (proposal `2026-09-26-console-accounts-connectors-secrets.md`):
  accounts, the authorization service, connection journeys, layered
  configuration and AWS secrets.
- Agent Y: `hub.call_jev` and its credentials.

## Problem

1. **Workflows run as a service identity nobody can sign in as.** A Python
   workflow acts as `workflow:<name>` (`workflows/runtime.py`, `run_scope`
   subject) and a markdown task as `workflow:md:<task>`
   (`schedule_runner.service_subject`). These subjects hold grants in the
   Console like a person, but they have no account, no email and no personal
   connections. A workflow that should read *my* Gmail, produce *my* report and
   email it to *me* has no "me".
2. **No way to hand a generated report to a person.** Chat artifacts live under
   `<hub>/chats/<chat_id>/artifacts/` behind an HMAC link that anyone holding
   the link can open (`_signing.py`, `server.get_artifact`). There is no owner,
   no sharing choice, no revocation short of rotating the hub secret, and no
   workflow API to publish a file.
3. **No email.** `on_failure` only posts to a webhook (`runtime._notify_failure`).
   There is no SMTP sender, no recipient rule and no delivery record.
4. **Workflow state is not partitioned by person.** `hub.state` is keyed
   `(hub, workflow, key)` (`workflows/state.py`). If the identity a workflow
   runs as changes, the next person inherits the previous person's state.

## Decisions

1. **Every execution runs as an ordinary Hubzoid account.** There is no service
   account type and no service mode. A team that wants shared automation
   creates an ordinary account for it.
2. **Identity precedence (scheduled runs):**
   1. `run_as` on the declaration (`@workflow(run_as=...)`, or `run_as:` in
      `schedule/*.md` frontmatter);
   2. `HUBZOID_WORKFLOW_USER` from the hub layer (`<hub>/.env`, or a hub secret);
   3. `HUBZOID_WORKFLOW_USER` from the deployment layer (gateway environment or
      deployment secret);
   4. the **setup default**: the configured initial owner, recorded once when
      Hubzoid provisions that owner (`GrantStore.provision_owner`). Locally
      (authentication off, no deployment) that is `admin@localhost`. In a shared
      deployment it is the configured owner account
      (`HUBZOID_GATEWAY_ADMIN_EMAIL`), never a hard-coded address.

   Adding users never changes the default. Hubzoid never picks an arbitrary
   administrator or the last person to sign in. Missing or unresolvable
   configuration fails the run with a message that names the fix. The one
   exception is legacy hubs (decision 15).

   An account is usable when all of the following hold:
   - it has a bound `hz_identities` row (an Open WebUI account id);
   - it is not pending, blocked or replaced;
   - on a Console-managed hub, it holds `use_hub`.

   Local quickstart mode (authentication off, no deployment) is the one case
   without a bound account. There, `admin@localhost` is the only possible
   account, and Hubzoid accepts it before Open WebUI first creates it.
3. **Identity is resolved once per run and checkpointed.** The first step of
   every run resolves the account (email to a stable `hz_identities` row with
   an Open WebUI account id) and stores `{subject, account_id, source}` as a
   DBOS step output. Recovery and retries reuse that output, so a
   configuration change never switches the person mid-run. Account status and
   permissions are checked again before every protected operation (restricted
   tools, model and agent calls, publishing, email, connections).
4. **`run_as` selects an identity. It grants nothing.** The run gets exactly the
   named person's current permissions. Authorship grants no permission and
   never lends the author's (or an admin's) connection. An authorization
   failure never falls back to another identity. `run_as` is read only from
   trusted files and operator configuration. No API, tool or model argument
   can set it, so a future agent or delegate API cannot turn it into
   impersonation. Future manual and chat invocation must run as the
   authenticated caller unless an explicit, authorized delegation exists.
5. **Legacy `workflow:*` grants are not translated.** They stay in the store,
   are no longer consulted for runs, and are reported: a run whose legacy
   subject held a permission its execution identity lacks logs a warning
   naming the permission and the fix. `hubzoid schedule list` shows who each
   workflow runs as. Migration is an explicit Console grant.
6. **Personal state is partitioned by execution identity.**
   - `hub.state` is keyed `(hub, workflow, owner, key)`. Rows written before
     this release (owner `''`) are adopted by the first identity that runs the
     workflow afterwards. Nothing is copied to anyone else.
   - `hub.shared_state` is the explicit, non-personal alternative for data that
     must survive a change of identity.
   - `hub.run_dir` is a private per-run scratch folder (`.hubzoid/runs/...`).
   - A markdown task's scratch folder (`.hubzoid/schedule/<task>/`) belongs to
     the identity that first uses it after upgrade. Another identity gets a
     sibling folder (`.hubzoid/schedule/<task>@<person>/`), so neither agent
     can read or write the other's state.
   - Settings (`workflows/settings.yaml`) stay shared configuration.
7. **Publishing is separate from generating.** `hub.publish_artifact(path,
   title=...)` copies an existing file into a private, per-artifact store
   (`<hub>/.hubzoid/artifacts/<id>/`), records owner, hub, workflow, run,
   content type, size, hash and sharing server-side, and returns a stable id
   and viewer URL. The owner is the run's execution identity, never a caller
   argument. Historical artifacts are never overwritten. No conversion service
   is built. A reusable HTML report template is provided.
8. **Viewer at `/portal/artifacts/<id>`.** It is served by any bridge through
   the existing `/portal` edge route, so no edge change is needed. The page has
   a thin toolbar (title, created time, Share, Download) with the content
   below:
   - HTML and PDF in a frame. HTML carries a CSP `sandbox` without
     `allow-same-origin`, so it runs in an opaque origin and cannot read the
     chat session or the toolbar. It also has no network access by default.
   - Images, CSV (as a table), and plain text or JSON (escaped) are previewed.
   - Everything else is download only.
9. **Authentication reuses the Open WebUI session** through
   `access/session.verified_email`. No Console privilege is needed to view or
   share your own report. A signed-out viewer is sent to
   `/auth?redirect=/portal/artifacts/<id>`, built only from the validated id.
   Open WebUI keeps `redirect` across password and Google sign-in in
   `localStorage`, and the pinned bundle has no route for `/portal/...`, so
   `goto` falls back to a full page load of the report.
10. **Sharing modes are exclusive per artifact:**
    - **Only you.** The default.
    - **Specific people or groups.** Eligible hub members: an account, or an
      Open WebUI or roster group, resolved at view time.
    - **Anyone with access to this hub.** Current `use_hub` holders.
    - **Anyone with the link.** A public bearer link with no sign-in.

    Sharing with people and hub members requires a Console-managed hub,
    because a legacy hub's membership lives in the chat app and Hubzoid cannot
    verify it. Public links require the new capability `share_public_links` in
    that hub, checked when the link is created or rotated and on every open.
    Links are 256-bit random tokens, stored only as a SHA-256 hash, with an
    expiry (default 7 days, maximum 90), revocation and rotation. The token
    travels in the URL fragment (`/portal/p/#<token>`), so it never appears in a
    request line, access log or `Referer`. The page exchanges it for a
    short-lived, path-scoped cookie. A workflow or model can never create a
    public link. Owner rights: view, download, manage sharing, revoke and
    delete. Viewer rights: view and download. There is no editor role. Admin or
    workflow-manager status reveals nothing. Changing one artifact's audience
    never changes future ones. `publish_artifact(share_with=...)` is the explicit
    per-call audience for authenticated modes only.
11. **Email is owner-only.** `hub.send_email(subject, body, artifacts=[...])`
    sends to the execution identity's own approved account email. There is no
    `to`, `cc` or `bcc`. Artifact references must belong to that owner and
    become authenticated viewer links. It sends through a deployment-wide SMTP
    sender (`HUBZOID_SMTP_*`), with hub overrides and AWS secrets arriving
    through Agent X's layered configuration.
    - **Refused, not reported as success:** missing SMTP settings, and unusable
      recipients such as `admin@localhost`, single-label domains and pending
      or blocked accounts.
    - **Preview:** `HUBZOID_EMAIL_DELIVERY=preview` writes an `.eml` to a private
      outbox and says plainly that nothing was sent.
    - **TLS:** STARTTLS or implicit TLS, with the default certificate and
      hostname verification. Credentials are never sent over a plaintext
      connection.
12. **Delivery semantics:**
    - Each send is a DBOS step with a delivery row keyed by `(run, step)`.
    - The row moves to `sending` only after the recipient is accepted, just
      before `DATA`.
    - A retry after a failure that happened before `DATA` resends, because
      nothing could have been accepted.
    - A crash or disconnect after `DATA` is recorded as **ambiguous** and is
      never resent automatically.
    - A step that re-executes after the server accepted the message returns the
      recorded result and does not send again.
    - `accepted` means the SMTP server accepted the message. It does not mean
      the message reached the inbox. SMTP gives no exactly-once guarantee, and
      Hubzoid does not claim one.
    - The `Message-ID` is derived from the delivery id, so a duplicate is
      recognizable.
13. **Connections.** The run binds the execution identity (surface
    `workflow`) for its whole duration, so Agent X's per-turn connection
    resolution selects that person's connections, never an administrator's.
    `hub.connection(app, ref=None)` returns the execution person's credential
    for Python steps:
    - it refuses when the person has several active connections and no `ref`;
    - it verifies that a `ref` belongs to that person;
    - an expired or revoked connection fails with a clear reconnect message.

    The credential object redacts itself in `repr` and refuses to be pickled,
    so returning it from a step fails loudly instead of checkpointing a secret.
14. **Markdown tasks can opt in to two schedule-only tools**, `publish_artifact`
    and `send_email`, with the same owner and recipient rules.
    - The task enables them itself with `publish_artifacts: true` and
      `send_email: true` in its frontmatter. The file is the deliberate
      delivery configuration.
    - Existing tasks are offered nothing new.
    - The model cannot choose the owner, the recipient or the audience.
    - The model can publish only files under the task's writable paths.
15. **Legacy hubs stay as they are.** "Legacy" means a hub whose access is
    still managed in the chat app.
    - **No chat-app groups.** The execution identity carries no Open WebUI
      groups there, so restricted tools stay unreachable in scheduled runs,
      exactly as today. Restricted tools in scheduled runs need a
      Console-managed hub.
    - **No resolvable identity.** When nothing resolves (no `run_as`, no
      `HUBZOID_WORKFLOW_USER`, no provisioned owner), the run keeps its
      previous service identity (`workflow:md:<task>` or `workflow:<name>`).
      A warning names the fix, and the run is recorded with source
      `legacy-service`.
    - **What still needs a real account.** Publishing, email and personal
      connections refuse to run without one.
    - **Invalid configuration still fails.** A `run_as` or
      `HUBZOID_WORKFLOW_USER` naming a missing or blocked account fails the run
      with no fallback.
    - **Console-managed hubs** always need a real account.

## Configuration

| Key | Layer | Default | Meaning |
|---|---|---|---|
| `HUBZOID_WORKFLOW_USER` | hub or deployment | setup default | Account email a scheduled run acts as when the declaration has no `run_as`. |
| `HUBZOID_SMTP_HOST` | deployment (hub override) | unset | SMTP server. Unset: email is refused unless preview mode is on. |
| `HUBZOID_SMTP_PORT` | same | 587 (465 with SSL) | |
| `HUBZOID_SMTP_USERNAME` / `HUBZOID_SMTP_PASSWORD` | same, secret | unset | Credentials, sent only over TLS. |
| `HUBZOID_SMTP_FROM` | same | unset (required) | From address. |
| `HUBZOID_SMTP_STARTTLS` | same | true | Upgrade with STARTTLS. |
| `HUBZOID_SMTP_SSL` | same | false | Implicit TLS (port 465). |
| `HUBZOID_EMAIL_DELIVERY` | same | `smtp` | `smtp` or `preview`. |
| `HUBZOID_ARTIFACT_MAX_BYTES` | hub | 50 MiB | Largest publishable file. |
| `HUBZOID_ARTIFACT_LINK_DAYS` | deployment | 7 (max 90) | Default public-link lifetime. |
| `HUBZOID_ARTIFACT_ALLOW_ORIGINS` | deployment | unset | Extra origins a published HTML report may load scripts, styles, fonts and images from (for example a chart CDN). |

## Data (migration `op_0005`, forward only)

- `hz_workflow_kv.owner`: a new column, now part of the primary key. Existing
  rows get `''`.
- `hz_artifacts`: `id`, `hub`, `owner`, `owner_account`, `workflow`, `run_id`,
  `idem_key` (unique), `title`, `filename`, `content_type`, `size`, `sha256`,
  `storage`, `audience`, `created`, `updated`, `deleted`.
- `hz_artifact_shares`: `artifact_id`, `kind` (`user` or `group`), `principal`,
  `added_by`, `added`.
- `hz_artifact_links`: `id`, `artifact_id`, `token_hash` (unique), `created`,
  `created_by`, `expires`, `revoked`.
- `hz_email_deliveries`: `id`, `idem_key` (unique), `hub`, `workflow`, `run_id`,
  `owner`, `recipient`, `subject`, `artifacts`, `mode`, `status`, `attempts`,
  `detail`, `smtp_code`, `message_id`, `created`, `updated`.

Content files live under `<hub>/.hubzoid/artifacts/`, which is already private
to agent file tools (`_fs._PRIVATE_DIRS`), unreachable from the legacy
`/artifacts/<chat>/<file>` route, and included in `hubzoid backup`. Link tokens
are stored hashed, so a backup holds no usable link.

## Integration contracts with Agent X

The plan uses what exists today and names what X's packages must provide:

1. **Authority.** Artifact and email checks call `GrantStore.can`,
   `is_suspended` and `identity` (the store X's `AccessService` wraps). The new
   capability `share_public_links` is added to the built-in permission catalog
   (`deployment.permission_catalog`), so `AccessService.catalog` picks it up.
2. **Session.** The viewer uses `access/session.verified_email` and
   `require_same_origin` (P0).
3. **Configuration.** X's `BRIDGE_DEPLOYMENT_KEYS` (P3) must add
   `HUBZOID_WORKFLOW_USER`, `HUBZOID_SMTP_*`, `HUBZOID_EMAIL_DELIVERY`,
   `HUBZOID_ARTIFACT_LINK_DAYS` and `HUBZOID_ARTIFACT_ALLOW_ORIGINS`.
   `child_env_overrides` must blank `HUBZOID_SMTP_PASSWORD` and
   `HUBZOID_SMTP_USERNAME`. Until P3 lands, bridges inherit the gateway
   environment, and these keys already reach them.
4. **Connections.** P2's `connector_<app>` gate and personal-token injection key
   on `current_identity()`. The run binds the execution identity on the
   `workflow` surface, which is in the default restricted surfaces. For `ref`,
   this plan uses the broker's `active_account_ids(user, app)` when present (P2)
   and otherwise a Composio lookup that checks the account's `user_id`.
5. **Delegation.** X's `AccessService.scope` already gives `workflow:*` subjects
   no management scope. With real accounts, the management tools still refuse
   the `workflow` surface (`TOOL_SURFACES`).

## Boundaries

- Workflow Python, `run:` scripts and restricted tools run in the bridge
  process as the same OS user. Nothing here is an OS sandbox. A trusted
  operator who can edit `workflows/` or `.env` can read what the process can.
  The identity rules protect people from each other through Hubzoid's
  surfaces: chat, agent tools, the viewer, email and the Console. They do not
  protect against the operator.
- HTML isolation depends on the browser honouring CSP `sandbox`. PDF preview
  uses the browser's viewer without a sandbox, because Chrome refuses to render
  sandboxed PDFs.

## Non-goals

- A Console Run button, workflow creation from chat, a visual builder, or a new
  Console execution flow.
- Chat or manual invocation that uses `run_as`.
- Sharing by email domain.
- Recipients other than the owner, and attachments.
- A conversion service.
- Listing artifacts in the Console.

## Testing plan

Deterministic and model-free first (`tests/`), serially at the end:

- **Identity:** precedence (explicit, hub, deployment, setup default).
  Bootstrap in local mode and authenticated mode. Adding users does not change
  the default. Missing, pending, blocked and replaced accounts. No fallback.
  The legacy-grant report.
- **Python workflow on DBOS:** `run_as`. Identity checkpointed across
  recovery while the configuration changes. `hub.state` partition and legacy
  adoption.
- **Markdown task:** `run_as` frontmatter, the identity bound in rounds, a
  scratch partition per identity, and the tools under both runtime adapters.
- **Two people run the same workflow:** state, artifacts, email recipient and
  connection all stay separate.
- **Artifacts:** each sharing mode, immediate revocation, eligibility, the
  legacy-hub refusal, public-link expiry, rotation and permission loss,
  direct-download and alternate-route authorization (legacy `/artifacts`,
  agent file tools), HTML CSP, and HTML, PDF, CSV, image and other-file
  behaviour.
- **Web:** login redirect, return URL validation, an ordinary user without
  Console privileges, same-origin mutations, and public links with the token
  only in the fragment.
- **Email:** fixed recipient, missing configuration, unusable recipient,
  preview, SMTP failure before and after `DATA`, ambiguous no-resend, and
  idempotent re-execution. Run against a local fake SMTP server.
- **Local end-to-end sample:** synthetic per-user data, then a report from the
  template, publish, and preview email, as a real DBOS workflow run.
- **Real browser, once:** the Open WebUI sign-in return flow and the viewer
  against a disposable local hub, with Playwright.

Real Google OAuth, real SMTP providers, real Composio accounts and AWS secrets
are **not** exercised. They are reported as unverified.

## Definition of done

- A scheduled Python workflow and a markdown task each run as the account
  given by `run_as`, the hub or deployment `HUBZOID_WORKFLOW_USER`, or the setup
  owner, and the account is shown in `hubzoid schedule list`.
- A missing default fails with a fix-naming message.
- A workflow publishes a report that only its owner can open. The owner shares
  it with a teammate, then with the hub, then by public link, and then revokes
  each. Access changes on the next request.
- `send_email` reaches only the owner. It refuses without SMTP, previews
  explicitly, and never resends an ambiguous send.
- The full suite passes.
- No existing hub changes behaviour unless it uses the new APIs. The one
  exception is the documented identity switch from `workflow:*` subjects.
