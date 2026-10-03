# Release review hardening and concise operator UX

Status: authorized for implementation and commit by the owner on 3 October
2026, after reviewing the production-readiness report for candidate 6a5d165.
The owner requested focused validation, not another full suite run.

## Need and scope

The human review reproduced attachment collisions, rehearsal writes through
symlinks, audience widening of imported restricted shares, and false gateway
readiness/success after a child failed. These must be fixed before release.
The normal chat, multi-hub, workflow and backup journeys worked, with bounded
archive, share-route, startup and documentation fixes already prepared.

1. Verify attachment content before reuse. Allocate stable collision names,
   preserve files and support idempotent imports.
2. Materialize rehearsal symlinks into the scratch copy and enforce its write
   boundary. Preserve consistent SQLite copies. Reject unsafe destinations.
3. Keep migrated owner-only/restricted share links closed until the owner
   explicitly republishes. Preserve metadata and public links. No new share
   ACL system or database schema is needed.
4. Validate ports and owned process readiness in both gateway modes. Verify
   the hub identity of independently managed bridges. Fail startup or a
   supervised child exit with a nonzero status and clean up child services.
5. Render workflow results as structured data and artifact links. Reuse
   existing CLI controls through copyable commands. Hide secondary explanations
   behind accessible question-mark help and raw identifiers behind details.
   Failures and privacy/consent boundaries stay visible.
6. Move SDK object construction out of neutral loaders and eval logic into
   existing runtime factories. Preserve loader outputs and injected judges.
7. Update the canonical project rules, operating/upgrade docs and release
   record. Keep SQLite the single-host default with separate per-hub workflow
   databases, explicit recovery checks and no multi-host/HA claim.
8. Reduce ordinary framework startup chatter and register readable standalone
   Open WebUI model names without changing model ids or access grants.

## Build order and validation

Migration fixes first, then process lifecycle and runtime adapters, then UI and
docs. Extend existing regression fixtures, remove resolved strict xfails, run
the affected Python checks including real Open WebUI and PostgreSQL fixtures,
build the frontend and run its pure data checks. Recheck the local browser
journeys and installed package behavior. Commit the reviewed source and bundled
frontend together. No PR, push or production deployment is requested.

## Decisions and limits

The only current customer already uses Console-managed access. Do not retain
or restore a second access authority. Open WebUI mode and its native connector
tool names remain supported. The first customer upgrade can keep that UI,
avoiding unnecessary history/token migration. Native UI migration retains its
documented subset and requires per-person connector reconnection.

No new execution engine, workflow canvas, feature gate, auth framework, or
memory system. No customer endpoints or credentials are exercised. Real SSO,
customer integrations, a human new-password flow, and actual device acceptance
cannot be certified by model-free fixtures. Record those limits honestly.


## Implemented and rechecked

The fixes above are implemented without a new database schema or required hub
field. Structured outputs preserve the existing privacy checks. Artifact
actions use only the local canonical viewer route. Existing gateway bridges
remain independently managed with `--no-bridges`.

Focused validation on the patched candidate:

| Check | Result |
|---|---|
| Migration and share privacy, with installed Open WebUI and PostgreSQL fixtures | 37 passed; resolved strict xfails removed |
| CLI, gateway, factories, server and workflow presentation | 277 passed |
| SQLite crash recovery, backup/restore and actual occupied ports | 31 passed |
| Neutral loaders, cross-hub runs and per-person MCP parity across runtimes | 76 passed; 2 real Codex CLI checks skipped |
| Judge adapter, model completion and failure handling | 51 passed |
| Frontend | TypeScript/Vite build and pure stream/router/image/result checks passed; lint has no errors |
| Live gateway failure | Two SQLite hubs started; stopping one bridge caused exit 1 and released all three owned ports |
| Browser | Eight-hub demo; compact workflow list and result; report rendered; question-mark help and command-copy worked; archiving the active synthetic conversation reset to an empty chat |

Counts overlap where existing test modules support more than one change. The
full suite was not repeated, as requested. The browser viewport override did
not take effect (it remained 1280 pixels), so no real-phone claim is made.
Customer SSO/integrations, real Codex acceptance and a human new-password
journey remain deployment acceptance checks, not completed by these fixtures.

## Approved launch follow-up · 3 October 2026

The owner authorized fixing and committing the independent branch review's
remaining findings. Record eval prompts with their run instead of joining live
case definitions; delete native account chat history and shares transactionally;
supervise standalone public services and handle gateway shutdown during startup;
enforce canonical phone ownership with a database uniqueness migration; accept
delayed workflow cancellation; reconcile operating notes with these behaviors.
No new required hub fields or runtime-specific loader behavior is introduced.
The phone migration stops on ambiguous existing assignments rather than choosing
an owner. Repeat focused privacy, SQLite/PostgreSQL concurrency and process
failure checks; the owner requested no further full suite run.

## Launch follow-up completed

All seven independent findings (A01–A07) are addressed. Eval history uses saved
inputs; native deletion removes chat records and shares in the account
transaction, cleans registered chat files, and denies legacy orphan links.
Standalone and gateway services now have consistent readiness, failure and
shutdown handling. The new `op_0017` migration enforces unique canonical phone
ownership on SQLite and PostgreSQL. Delayed workflow runs are active across
controls and status filters. Release and contributor documentation matches the
implemented behavior.

| Focused check | Result |
|---|---|
| Eval privacy, native deletion, chat stores, migrations, access and workflow controls | 190 passed, including SQLite and PostgreSQL |
| CLI and both gateway modes | 141 passed |
| Phone concurrency/upsert and standalone Open WebUI exit/readiness checks | 7 passed (overlaps the access checks above) |
| Invalid phone input preserves the existing assignment | 3 passed |
| Actual occupied-port refusal | 2 passed |
| Real standalone edge failure and gateway SIGTERM after bridge startup | 2 passed; expected exit codes and all owned groups/ports released |
| Installed Open WebUI history/markup migration | 39 passed |
| Console account/phone routes, native account adapter and channel identity | 64 passed |

No full suite rerun, customer deployment or publishing was performed. The
concise frontend and earlier human browser review remain in the preceding
hardening commit. Customer sign-in and integrations need deployment acceptance;
real phone viewport, live SSO, authenticated Codex and release-image checks
remain unverified, as recorded above.

## Approved restart and final UX follow-up

The owner requested verification and correction of the reported CI and restart
regressions. Match the server's address-reuse behavior in port probes; retain
hub identity checks while accepting pre-model health responses and retrying
mismatches to the startup deadline. Name the failed hub and document independent
bridge upgrade order. Keep gateway-owned services as one supervised unit;
document independent services for isolated restarts and systemd control-group
cleanup for OOM or forced kills. Do not add a custom restart daemon or kill
unknown listeners. Correct stale test fixtures and status expectations. Fill
workflow commands with registered server paths, keep the timezone visible, and
remove duplicated readiness wording only where reproduced. Rebuild the shipped
frontend and run the affected tests plus real socket/process checks. The owner’s
instruction against a full-suite rerun remains in force; the separate default
suite report supplies broader regression evidence.

## Restart and final UX follow-up completed

The five reported CI failures were reproduced and their stale fixtures corrected.
The bridge-port probe now uses the server's address-reuse behavior, allowing an
immediate restart after TCP connections close while still rejecting an active
listener. Gateway identity checks accept older health responses without a model,
retry malformed/mismatched responses to the deadline, and identify the failing
hub and expected model. Existing independently supervised bridges should be
upgraded/restarted before the gateway.

Run, cancel, create and webhook-redrive commands use the registered absolute hub
path, with shell quoting. The schedule timezone is visible; the raw cron and
secondary instructions remain in accessible help. The browser fixture now
serves the read-only webhook view and includes delayed runs in its active filter.
Open WebUI's gateway-ready message was not duplicated in the reviewed path; an
existing CLI test now verifies it occurs once. The factory spacing is corrected.

Gateway-owned services deliberately remain one supervised unit. Independent
bridge services with `--no-bridges` retain isolated restarts. Deployment guidance
now explicitly covers systemd control-group cleanup and OOM policy. A manually
force-killed CLI cannot guarantee child cleanup; no custom supervisor or automatic
termination of unknown listeners was added.

| Focused check | Result |
|---|---|
| Branding fixtures, cross-hub run filters, CLI, gateway modes and Console API | 180 passed |
| Actual recently closed TCP restart and occupied-port refusal | 3 passed |
| Actual service failure and SIGTERM during gateway startup | 2 passed; expected exit codes and owned ports/groups released |
| Open WebUI gateway readiness count | 1 passed (included in the CLI scope above) |
| Frontend | TypeScript/Vite build and command/result checks passed; lint has no errors (existing warnings remain) |
| Browser | Synthetic workflow list showed timezone and help; command copy produced success feedback; no view errors after completing the fixture |

No full suite rerun, customer deployment, push or publishing was performed. The
separate default suite review supplied broader regression evidence before these
focused corrections. Customer sign-in and integration acceptance, and the
previously recorded real-device/SSO/Codex/release-image limits, remain unchanged.

## Approved MCP consent UX follow-up

The owner requested a human review of the MCP OAuth pages within the authorized
release UX cleanup. The isolated browser rehearsal confirmed a dated, prose-heavy
consent page, an internal folder name in place of the agent name, no direct
revocation navigation, and unstyled browser errors. Keep the server-rendered page
and the existing FastMCP protocol, sign-in dispatch, resource scope, CSRF checks,
grant lifetime and revocation logic. Reuse the MCP agent's display name and the
chat app's visual tokens. Present the assistant, account, hub, action permissions
and callback host clearly; keep the client-name trust warning visible and move
technical explanations into keyboard/touch-accessible help and native details.
Style expired, denied and invalid-request states with local recovery navigation.
Show creation/expiry dates on assistant connections. No external client logo,
JavaScript, new permission, new credential handling or extra UI dependency.
Run the existing OAuth and account-dispatch checks, extend security-relevant
metadata/error coverage, and recheck consent, cancellation, connections and
expired/denied states in the isolated browser fixture. Commit the verified fix.

## MCP consent UX completed

The consent and connection pages now match Hubzoid's current visual tokens and
show the same agent name as the MCP server. The assistant identity, signed-in
account, action permissions, callback host and client-name trust warning remain
visible. Secondary explanations use keyboard-accessible question-mark help;
full endpoint details use a native disclosure. Consent actions fit the reviewed
desktop viewport. Assistant connections show creation/expiry dates and revocation
guidance. Browser errors preserve their status and security headers while adding
useful local recovery links. No protocol, token, grant or sign-in policy changed.

Validation: 63 focused tests passed across OAuth lifecycle, metadata escaping,
denied access, native/Open WebUI account dispatch, MCP authorization and grant
reconciliation. The isolated browser verified sign-in returning to consent,
keyboard help, cancellation, the revocation link and dates, revocation of a
synthetic connection, and expired/denied recovery pages. No real assistant was
authorized and no customer account or endpoint was touched. No full suite was
rerun. Real-device/dark-theme and customer integration acceptance remain outside
this focused browser check.

During final push, the release branch had acquired a concurrent MCP consent
redesign. Integration retains that newer permission summary and visual layout,
adding this follow-up's recovery navigation, accessible technical details and
connection dates. The 63-test and browser results above describe the follow-up
before this integration; no additional tests or browser runs were performed
after the owner's explicit request to close and push without further tests.
