# First-use stabilization verification

Reviewed 25 September 2026 on `auth-schedules-upgrade`, based on `a197727` plus
this working tree. This is candidate evidence, not a published-release claim.
Scope and the owner's decisions are recorded in
[the proposal](../proposals/2026-09-25-first-use-stabilization.md).

## Approved review items

| Item | Implemented outcome / decision |
|---|---|
| UX-01 | The verified configured owner gets administration and hub entry once; revocation survives later sign-in. |
| UX-02 | The agent picker applies Hubzoid entry rules to administrators and members and fails closed when access cannot be checked. |
| UX-03 | Startup refreshes owned saved bridge URLs/keys without clearing other saved configuration. |
| UX-04 | Deferred by owner: historical workflow-history migration. Preserve existing databases and backups. |
| UX-05 | Workflow scaffold prints the actual `schedule run <hub> <workflow>` command. |
| UX-06 | Owner decision: suppress upstream changelog; no replacement welcome or tour. |
| UX-07 | One existing chat account is the initial owner; Console link becomes available after verified setup. |
| UX-08 | Denial copy identifies the account and gives the applicable access/retry next step. Denied agents are removed from the picker. |
| UX-09 | Console naming and agent display/model IDs align; agent details link directly to that agent's chat. |
| UX-10 | Access copy distinguishes chat entry, tool capabilities and administration. |
| UX-11 | Account-joining guidance explains signup/approval; granting access does not pretend to send an invitation. |
| UX-12 | Grant form distinguishes people from service identities, including `workflow:md:<task>`. |
| UX-13 | Standalone deployments do not report irrelevant gateway sync warnings; refresh guidance describes actual behavior. |
| UX-14 | Entry and administration capabilities have useful descriptions. |
| UX-15 | Shared compact alert treatment uses the current Ant Design title/description structure. |
| UX-16 | Sign-in, permission and account-service failures retain distinct status and recovery guidance. |
| UX-17 | Studio light/dark tokens, readable accent text and semantic states. |
| UX-18 | Labelled theme controls, main landmark, heading hierarchy, keyboard and mobile browser checks. |
| UX-19 | Existing brand assets preserved; Inter is local; font fallbacks and font licenses ship with the package. |
| UX-20 | Owner decision: retain useful native chat controls and their existing groups. No replacement chat settings system. |
| UX-21 | Tool-start marker is neutral; errors without detail have a meaningful fallback. |
| UX-22 | Minimal hello skill supplies the tool's required argument; fresh real chat completes without a failed first attempt. |
| UX-23 | Markdown-first workflow guidance, correct copyable CLI commands and a model-free manual Python scaffold. |
| UX-24 | Manual, disabled-in-file, disabled-deployment, paused and scheduler-health states remain distinguishable. |
| UX-25 | Common cron expressions have readable descriptions; raw cron and actual timezone remain visible. |
| UX-26 | Owner decision: Console execution/recovery controls remain deferred. CLI operation is documented. |
| UX-27 | Pretty results, bounded/wrapped output, inspectable steps and copyable run IDs. Lists shorten IDs visually without losing the full value. |
| UX-28 | Agent/workflow changes reset run pagination; explicit refresh and snapshot timing; existing live Runs mode retained. |
| UX-29 | Forwarded chat IDs are used; title/suggestion calls are background usage, excluded from human conversation/message counts. |
| UX-30 | Cost help explains estimates, unpriced calls and subscription billing; tokens include background calls. |
| UX-31 | Owner follow-up: merged Agents landing with five summary cards, conversation subcount and agent cards; removed overview prose and global Runs navigation. |
| UX-32 | Singular/plural counts and capability labels are explicit. |
| UX-33 | Owner decision: preserve upstream admin features; explain ownership in operator docs and code comments. |
| UX-34 | Python/runtime prerequisites, first-run paths and standalone scaffold commands are copyable. |

## Verification evidence

- Final frozen model-free Python suite: **1,619 passed, 4 skipped,
  4 deselected** in 795.99 seconds, using the requested development environment.
- Real-provider/delegation/upload and related first-use regressions passed
  **38/38** after isolating their test credentials. These are separate checks;
  counts are not additive with the full suite.
- The affected access/safety/portal group passed **87/87**; PostgreSQL acceptance
  passed **10/10**. The final full suite includes the additional owner revocation
  and subprocess/SQLite shutdown regressions.
- Final Console build and **34 browser journeys passed**, covering access
  changes and recovery, blocked users, empty/error states, filters, pagination,
  refresh, keyboard focus, mobile navigation and chat navigation visibility.
  The browser test closes its browser in `finally`.
- Fresh hub: real first chat used `load_skill` and `hello` successfully. No
  upstream changelog appeared. The owner opened Console without a second
  account/bootstrap command. One real conversation counted as one message.
- Fresh manual Python scaffold ran successfully; result and recorded step were
  visible in Console. Reviewed Overview, Agents, access editor, People and its
  drawer, global/agent Activity, workflow catalog, run list and run detail.
- Synthetic Docker upgrade and rollback: **16 checks passed**, including
  continued scheduled work, retained account/model access and return to 0.9.6.
  The supervisor now waits for child shutdown. A leftover SQLite journal was
  safely checkpointed with all writers stopped before restore; the runbook
  documents this and matching environment restoration. No journals were deleted.
  The final restore-message clarification passed **25 backup tests** afterward.
- Wheel built in an isolated build environment. Inspected for Console assets,
  local font and both font license notices. Local README/docs links and
  `git diff --check` passed.

Tests used the requested shared development environment. Its unrelated installed
packages have existing dependency conflicts; use a clean environment or the
locked Docker image for deployment. The separate release environment's dependency
check passed. Do not treat a passing source test as a production cutover.

## Documentation and release boundary

README, quickstart, administration, managed/legacy access, authentication,
deployment, upgrade, workflow, design and documentation-index guides are aligned
with the product brief. The changelog includes this stabilization work.

Website documentation is being edited separately and was not modified here.
Before public publication its new routes/content should be checked against the
selected release. Website links complement the revision-specific local guides.

No production migration, merge, tag, package publication or deployment was
performed. Historical workflow migration and Console run/recovery buttons remain
explicitly outside this approved scope. A rollout still follows the backup,
quiet-window, account/access and rollback checks in [UPGRADING.md](UPGRADING.md).

## Owner follow-up: compact Agents landing

The requested merged layout replaces the former Overview table and separate
Agents listing. Five cards show messages/conversations, users, tokens, workflow
runs and approximate cost; the update time sits beside Refresh. Runs remain
inside each agent. Console account-management shortcuts are removed; public
email/SSO registration defaults to off, with administrator-created accounts and
explicit operator overrides preserved. The earlier full-suite and Docker results
above precede this follow-up; its focused verification is recorded separately.

Follow-up checks: **83 Python tests passed** (Open WebUI environment/config and
first-use behavior), Console build passed, and **34 browser journeys passed**
with new checks for the five-card row, period totals, refresh failure/recovery,
simplified navigation and mobile overflow. Reviewed the final light/desktop and
dark/live layouts; the live test hub reports public sign-up disabled. Test
browsers exited after completion. This follow-up does not change the earlier
release boundary.

## File-read security follow-up

The reported dotenv/database exposure and recursive-search bypass were reproduced
using synthetic canaries (23 failures in the initial 24-case reproduction set).
Built-in chat/MCP file readers now share a fail-closed path policy; both search
backends filter every file before reading. It covers dotenv variants, database
files/sidecars, SQLite signatures, private state and symlink targets. Knowledge,
skill and agent loaders cannot publish protected files through aliases. Current
chat uploads/artifacts remain usable and retain the secret-file checks. Jinja
uses its advertised sandbox to prevent object-traversal file-read bypasses.

- Broad affected group: **204 passed, 3 skipped**.
- Full model-free suite: **1,655 passed, 4 skipped, 4 deselected** in 674.86 seconds.
- Final focused checks: **51 passed**, including authenticated MCP transport,
  both search backends, overflow safety and the final ripgrep truncation notice.
  This last run covers extra regressions added after full-suite collection; the
  counts overlap and are not additive.
- Final wheel contents match the patched runtime modules; `git diff --check`
  passed. The local test hub was restarted with the fix, preserving its data.
- No production deployment was performed. Existing deployments must upgrade and
  restart their bridges/MCP servers. Custom operator-installed Python/integrations
  retain their own data-access responsibility; see [SECURITY.md](../SECURITY.md).

## PostgreSQL MCP follow-up

The SQLite-only key and group readers ignored Open WebUI's `DATABASE_URL`.
They now share the SQLAlchemy accessor used by connected MCP OAuth tokens and
server configuration. SQLite and PostgreSQL use the same queries and denial
rules. Read connections reject writes; only OAuth refresh opens a write
connection. Missing, unavailable or conflicting databases deny access without
falling back to stale local credentials. Gateway manifests register the shared
URL/schema while preserving the path used to locate uploads.

The regression fixtures use synthetic accounts and credentials. Real PostgreSQL
checks cover both the Docker profile's default schema and `DATABASE_SCHEMA`.
They exercise authenticated MCP calls, group-restricted tools, key/group
revocation, pending/expired/suspended accounts, account replacement, encrypted
OAuth refresh, registered deployments and read-only enforcement. SQLite runs
the same contract. The existing migration CLI fixture now uses a private hub;
the upload fixture restores its environment instead of disabling later tests.

- Initial affected group: **141 passed**, including existing PostgreSQL
  acceptance tests.
- Final backend/gateway/CLI group: **55 passed**. The counts overlap.
- Full model-free regression run: **1,688 passed, 4 skipped, 26 deselected**
  in 452.51 seconds. This run preceded the small curator/UI follow-up below.
- Upload/migration/scheduler isolation sequence: **23 passed**.
- The wheel builds and its seven changed runtime modules match the source.
- Local bridge, chat and Console returned HTTP 200 after restart with existing
  data retained and schedules disabled. No production deployment was performed.
- No browser test process was started for this database change. Temporary
  PostgreSQL fixtures shut down their own servers.

## SDK and Console follow-up

- Confirmed the scaffold's hub-first run command, Markdown service identity
  validation, Console title and five-card landing were already corrected.
- Added the built-in `curator` catalog entry (Save shared knowledge), available
  for people and `workflow:md:<task>` service identities. Verified grant/revoke
  behavior and hub isolation through the Console API.
- Corrected padded PNG wordmark sizing in the compact header and sidebar.
  Warning helper text passes a 4.5:1 rendered contrast check in both themes.
  The broader dark palette remains a documented suggestion.
- Updated MCP client examples/auth rules, backend selection, curator/service
  guidance, current navigation labels and the workflow function-name example.
  Website sources remain untouched while their separate edit is in progress.
- **76 focused SDK/API tests passed; 36 browser journeys passed**, including
  a Markdown service receiving curator through the complete Console flow.
  Console build and lint passed; reviewed the live header visually.
- The final wheel contains 219 package files matching current sources/assets,
  with no obsolete cached bundles. Test browsers and PostgreSQL fixtures exited.
  The manual review hub remains available on port 3092 with schedules disabled.
- Codex/Hermes transport support is documented from official client references;
  individual native clients were not exercised end to end. At this checkpoint a
  Codex execution backend and automatic first-run runtime choice were still
  proposals. Both ship in 1.0.1: see the Codex runtime section below and
  [providers](providers.md).

## Per-agent usage and manual access demo

- Agent cards now show input/output tokens and approximate USD cost for the
  same period as the dashboard totals. Unknown pricing remains explicit.
- Console build/lint passed; 36 browser journeys passed with per-agent period
  changes, partial prices and no-usage cases included. Reviewed the live dark
  dashboard and verified both sample capabilities appear unchecked in Add person.
- The private review hub on port 3092 contains two synthetic, read-only tools:
  `sample_inventory` (inventory) and `sample_report` (reports). No explicit grants
  were added. Organization administrators implicitly have all capabilities; use
  a regular chat account to test grant/revoke behavior. These fixtures are outside
  the package and are not customer data.

## Codex runtime, Apache and final dark theme — 25 September 2026

The owner authorized implementation of the local Codex backend, the refined dark
palette, and Apache-2.0 licensing. Website work remains with the separate website
task. Migration planning for existing deployments follows the public release.

- Full model-free suite: **1,702 passed, 4 skipped, 27 deselected** in 425.16s.
- Subsequent focused runtime/init/handover checks: **60 passed**, including the
  final code-mode isolation and delegate-usage changes. Counts overlap.
- Authenticated Codex smoke: **1 passed**, using synthetic knowledge and a
  synthetic dotenv canary. The model called permitted tools and dotenv reads
  remained blocked. CLI 0.147.0 is required; both direct and code-mode dispatch
  are also tested against a local synthetic Responses endpoint.
- Console build/type-check and lint passed. **36 browser journeys passed**,
  including rendered dark button/tag and both-theme warning contrast (at least
  4.5:1), desktop/mobile layout and access/error recovery. One intervening run
  timed out on the second Refresh click; a complete rerun passed. CI must pass
  again from the release commit. Test browsers close in a finally block.
- Apache LICENSE and NOTICE are included in wheel and sdist. All Hubzoid-owned
  license declarations use Apache-2.0; dependency license identifiers and font
  notices remain unchanged. Previously distributed versions retain their rights.
- An isolated wheel install (reusing an existing development environment's
  dependency packages) scaffolded a hub and served the packaged Console. This is
  not a fresh dependency-resolution test; the release workflow performs that
  check and the multi-architecture image build before publishing.

### Release boundary

This working tree is a tested release candidate, not a published release. Before
publishing: commit the reviewed branch, run CI on that exact commit, and run the
existing version-tag release pipeline (clean install, sdist/wheel and both image
architectures). The website owner must align public licensing and provider docs
with [LICENSING](../LICENSING.md) and [providers](providers.md). No website files
were changed by this task. No production deployment was migrated.

The local Codex backend uses an experimental, pinned protocol and requires a
file-backed service-account login. Hosted API providers remain the simplest
container path. Do not silently upgrade the CLI or switch existing hub models.

## Final Admin Console follow-up — 26 September 2026

Renamed the page and chat navigation to Admin Console. The chat link sits above
profile in expanded and collapsed sidebars. Capability descriptions use help
buttons while inherited/required states remain visible. Documentation separates
Open WebUI account creation from Admin Console permission grants.

Console build and lint passed. The compact capability changes passed 36 browser
journeys. Subsequent sidebar work passed 40 edge/admin Python checks and the
focused real-browser sidebar placement, replacement, keyboard and login/logout
checks. The final naming change passed 13 administration tests. Browser fixtures
now wait for cards before contrast measurement; Refresh has a stable accessible
name while its loading icon animates. Test browsers were closed. The local test
hub serves the updated UI. No release or production deployment has occurred.
