# Hand-back: auth-schedules-upgrade continuation (session 2)

Updated 2026-09-17. **STILL WORK IN PROGRESS — NOT READY TO MERGE.**
This continues `AUTH-SCHEDULES-HANDOFF.md`. Read that first; this file records
only what THIS session changed on top of it, what was validated, and what is
still open. No commits, no pushes were made. Working tree preserved and extended.

## Ground rules still in force

- Repo: `/Users/shreyarao/Desktop/WaveAssist/Hubzoid/HubZoid`, branch `auth-schedules-upgrade`.
- Python: `/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python`. Node v22.
- Do **not** push. Do **not** reset or discard the uncommitted work. No commit was
  requested; none was made.
- The twelve reviewed findings remain authorized to implement. Priorities unchanged:
  builder + end-user UX, thin integration, enterprise quality, multi-hub default,
  observable (not maximally durable) workflows.
- Keep OWUI as account/auth owner, Casbin as permission authority, DBOS as run-history
  authority. Do not add parallel systems.

## What this session did (all uncommitted, on top of the prior handoff)

### Scheduling observability (handoff "exact next steps" #3 — advanced)

- `hubzoid/workflows/observe.py`: added **stale-dispatcher detection**. New
  `_stale()` + `STALE_AFTER_SECONDS=150`. `catalog()` now classifies a scheduled
  workflow as `stale` when schedules are enabled but no dispatcher heartbeat has
  landed within the window (i.e. the scheduler process is stopped/crashed even
  though persisted health still says enabled). State selection rewritten from the
  dense ternary into explicit branches: `error` → `disabled` → `manual` → `stale`
  → `scheduled`. `catalog()` now also passes through a `downtime` field per row.
- `hubzoid/workflows/runtime.py`: added `downtime_missed(since, now)` — counts
  scheduled slots that fell in a downtime window, per workflow and total, capped
  at 100k iterations. **Reporting only — no back-fill** (durability priority is
  insight, not replay).
- `hubzoid/workflows/boot.py`: on dispatcher start, if a prior heartbeat exists and
  is >90s old, compute and persist a `downtime` record into runtime health and log
  a warning. Missed slots are surfaced, never executed.
- `hubzoid/workflows/runtime.py`: added `shutdown(completion_timeout_sec=5.0)` —
  clean DBOS teardown (`DBOS.destroy(workflow_completion_timeout_sec=...)`) that
  preserves recoverable runs, is best-effort (never hangs/crashes shutdown), and
  resets module globals so re-init is possible. `Dispatcher.stop()` now marks health
  disabled and calls `runtime.shutdown()` in a thread. This closes the handoff's
  "Dispatcher.stop cancels ticker only, not DBOS worker lifecycle" gap.

### Portal surfacing of the above

- `portal/src/api.ts`: `Workflow` type gains `downtime`.
- `portal/src/App.tsx`: `Status` badge now renders `stale` as danger; Workflows
  screen shows a **stale-dispatcher alert** and a **downtime notice** ("N runs
  missed during downtime … not back-filled"). No backend shape change needed —
  `/workflows` already returns catalog dicts verbatim.

### Code quality (handoff #8 — partially)

- Fixed the one portal lint warning: `useData` no longer calls `setState` synchronously
  inside the effect. Stale-hub protection is preserved by the existing
  `state.path===path?state:{}` render-time guard (documented in a comment). Lint is
  now **warning-free**.
- Ran `ruff --fix` on the whole changed/new Python set and hand-fixed the 3 remaining:
  removed an unused `dialect` (`store.py`), converted a `cols` lambda to a `def`
  (`migrate.py`), dropped an unused `n` (`boot.py`), plus auto-fixes for unused
  imports / f-prefixes across `cli.py`, `observe.py`, etc. **All changed/new files
  are ruff-clean.** (7 ruff errors remain in `hubzoid/` but ALL are in files this
  branch never touched — evals/assertions, factory_claude, scheduler, tools/*. Left
  alone per "no unrelated churn.")

### Docs / accuracy (handoff #5, #7 — partially)

- `README.md`: added a link from "Security and control" to `docs/ADMINISTRATION.md`,
  `docs/access-management.md`, `docs/DEPLOYING.md`, describing the two-journey
  multi-hub flow. (Root README was previously untouched — finding #12.)
- `hubzoid/db.py`: corrected the `operational_url` docstring, which previously listed
  a precedence that omitted the deployment manifest the code actually checks first.
  Now states manifest-authoritative behavior and the raise-on-disagreement rule.

### Tests

- New `tests/test_workflow_observability.py` (3 tests, model-free, no DBOS): `_stale`
  truth table incl. naive-timestamp and unparseable cases; `downtime_missed` counting
  across two hourly schedules + one manual (asserts no back-fill semantics); empty
  window. All pass.

## Validation performed THIS session

All run with the project venv. Nothing weakened; no test edited to hide a failure.

1. Targeted acceptance + core: `test_admin_journey, test_portal_api, test_access_store,
   test_migrate, test_workflow_integration, test_workflow_observability` → **54 passed**.
2. Broad affected slice `-k "reconcile or guard or enforce or deploy or schedule or
   workflow or access or owui"` → **343 passed, 1 skipped, 1084 deselected**.
3. **Real-DBOS** `test_workflow_durable_execution` ran (NOT skipped) and **passed** —
   this clears the handoff's flag that app-name hashing landed after the last DBOS run.
4. Portal: `oxlint` **warning-free**, `tsc -b && vite build` **passes**, browser journey
   `node tests/journey.cjs` **PASS** (hub-switch isolation, correct mutation target,
   recoverable errors, run details, access history, admin controls, ordinary-user denial).
5. `ruff check` on all changed/new files: **clean**. `git diff --check`: **clean**.
6. `hubzoid doctor demo-hub`: runtime builds (pre-existing unrelated AGENTS.md tools
   warning only). demo-hub has no workflows, so the workflow-validation branch of doctor
   was exercised only via the integration test, not doctor itself.
7. Portal build assets are consistent: `portal_dist/assets` holds exactly the one JS +
   one CSS that `index.html` references (vite empties the dir on build). The prior
   assets show as git deletes + new untracked names — expected; `git add -A` resolves it.

Current diff vs `main`: 24 files changed + new files (`tests/test_workflow_observability.py`,
plus the prior session's untracked set). ~1019 insertions / ~633 deletions.

## Still open (did NOT get to — carry forward from handoff, priority order)

1. **Edge HTML injection test (handoff #2).** The Manage-agent-access link injection in
   `portal_navigation.py` / `edge.py` still needs a real/mocked HTTP upstream test for
   response-path injection + cookie/header preservation. Existing edge test covers the
   lock and the JS endpoint, not full injection.
2. **Two-process duplicate-dispatch test (handoff #3 remainder).** The deterministic
   SetWorkflowID dedup is only proven with two calls in ONE process
   (`test_workflow_integration`). Prove it with two actual processes sharing one DBOS DB.
   Also: `shutdown()`'s clean DBOS teardown is NOT yet covered by a test — add one that
   starts, stops, and confirms no hang and that a recoverable run resumes on relaunch.
3. **Migration edge cases + rollback (handoff #4).** CSV-only path still lacks an
   independent effective baseline beyond its own plan; unknown/disabled OWUI models,
   owner/admin semantics, reserved CSV groups; snapshot schema validation + timing vs
   concurrent edits (documented short freeze still required); surface the expected
   effective matrix in CLI preview; OWUI ACL restoration on rollback-to-legacy is still
   a documented MANUAL step — consider integrating.
4. **Config precedence audit (handoff #5 remainder).** Per-hub `.env` can still influence
   DBOS/OWUI URL; validate precedence end to end; confirm no bootstrap can silently make
   existing customers "managed" globally; service credentials reachable by first bridge
   AND CLI sync; fail-clearly rather than silently choosing a different DB. `deployment.py`
   comments should be re-checked against actual behavior (db.py docstring done this session).
5. **API/store security & consistency (handoff #6).** policy_revision under concurrency;
   bulk/import reserved-grant validation parity with single grant; audit parity for
   restore/revoke_all/suspend/authority changes; unknown hub/permission + malformed
   mutation + last-admin race; blocked users must not retain access via legacy
   chat/MCP/tool routes; OWUI account delete/disable vs stale grants.
6. **Docs remainder (handoff #7).** Document the new env vars (`HUBZOID_OPERATIONAL_DB`,
   `HUBZOID_DBOS_DB`, `HUBZOID_SCHEDULES`, `HUBZOID_PORTAL_DEV*`, internal OWUI URL) in
   `settings.py` / the `.env` template; cross-link `docs/DEPLOYING.md` and
   `docs/access-management.md` to `ADMINISTRATION.md`; sweep remaining stale module
   docstrings and proposal claims.
7. **TS formatting (handoff #8 remainder).** `App.tsx` is still dense single-line JSX.
   Optional: apply an available formatter WITHOUT adding a runtime framework. Python
   format churn deliberately avoided.
8. **Full green run (handoff #9).** Run the COMPLETE pytest with loopback-socket
   permissions (the ~8 sandbox-blocked browser/edge tests need it), outside this session's
   sandbox. Then re-confirm portal lint/build/browser.

## Commands (unchanged from handoff, still current)

```bash
cd /Users/shreyarao/Desktop/WaveAssist/Hubzoid/HubZoid
/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python -m pytest -q \
  tests/test_admin_journey.py tests/test_portal_api.py tests/test_access_store.py \
  tests/test_migrate.py tests/test_workflow_integration.py tests/test_workflow_observability.py
/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python -m pytest -q -m 'not e2e'   # needs loopback perms
npm --prefix portal run lint && npm --prefix portal run build && npm --prefix portal test
/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python -m ruff check hubzoid/
git diff --check && git status --short
```

## Cautions for the next agent

- Do not claim "done" while section "Still open" has items — the two highest-risk are
  the migration effective-diff/rollback (customer impact) and the config precedence audit.
- `runtime.shutdown()` resets DBOS globals; it is only safe because init refuses two hubs
  per process. Keep that invariant if you touch it.
- The stale threshold (150s) and downtime threshold (90s) are heuristics tuned to the
  ~60s tick. If tick cadence changes, revisit both.
- Do not back-fill missed workflow runs — the design is deliberate (report, don't replay).
- Do not push; no commit was requested.

---

## Session 3: review of hand-back and further hardening (2026-09-17)

All earlier uncommitted changes preserved. No commit or push. This section
supersedes the earlier open-item status where explicitly addressed below.

### Findings fixed

- **Access import consistency:** bulk and migration used to skip malformed grants
  or accept public administrative grants that `grant()` rejected. All three now
  use `_validate_grant`; a bad batch fails before writing anything. Migration
  grants/attributes must stay within target hubs. The old test expecting silent
  wildcard skipping now asserts atomic refusal (intentional stricter behavior).
- **Rollback validation:** validate canonical hub/grant names, reserved scopes,
  boolean authority values and attribute scope before writes. CLI rejects a
  backup for another selected hub. Registered model IDs must match the hub on
  migration and visibility restore.
- **OWUI rollback implemented:** OWUI migration plans retain original model ACLs,
  included in mode-0600 cutover snapshots. Rollback to legacy restores those via
  OWUI REST while preserving current non-access model fields. API failure is
  explicitly partial success, exits 1, and can be retried with the same backup.
  Older/CSV-only snapshots still require a deployment backup for OWUI ACLs.
  Access edits and projection worker must be paused for rollback; no distributed
  transaction across Hubzoid and OWUI is claimed.
- **Effective preview:** shared candidate comparison runs in CLI preview as well
  as apply; prints decision count and differences. CSV-only warns that OWUI model
  entry is unverified. Disabled OWUI models fail preflight. Source SQL engines
  are disposed after reading. CSV-only independent legacy baseline remains open.
- **Configuration:** registered DBOS and internal OWUI URLs reject contradictory
  environment overrides, matching operational DB behavior. Manifest readers
  reject an unrelated hub path. Edge intentionally uses `require_hub=False`
  because it consumes the deployment from the gateway working directory.
- **Scheduler:** missing/corrupt heartbeat cannot report healthy scheduled state.
  Per-workflow dispatch errors are collected after trying other workflows and
  propagated into dispatcher health. Health-write failure no longer kills the
  loop. Empty/failed startup tears DBOS down instead of leaking it.
- **Proxy:** added compressed HTTP response integration test via mock transport:
  script injected once, both cookies preserved, security header retained, stale
  ETag/encoding removed, length correct, portal HTML untouched.
- **Documentation:** deployment/access docs link to ADMINISTRATION; settings
  module documents operational/DBOS/manifest/OWUI/schedules/dev-auth environment
  variables. Administration guide describes stricter config, rollback retries,
  preview limitations and supported SQLite topology.

### Validation

- Full model-free suite with approved loopback permissions before last rollback
  addition: **1,415 passed, 4 skipped, 21 deselected**. Final rerun recorded below.
- Latest focused rollback/migration/access/scheduler/process suite: **35 passed**.
- New real **two-process DBOS dispatch** test passes: same scheduled slot yields
  same workflow ID and exactly one recorded step effect. Both runtimes shut down.
  It initializes DBOS schema with an actual launch first. Simultaneous first-time
  SQLite schema creation raced in upstream DBOS (`dbos_migrations already exists`);
  this is NOT covered up as production support. Guide requires one SQLite bridge
  per hub; use PostgreSQL for multiple production workers. PostgreSQL not tested.
- Portal lint warning-free, TypeScript/Vite build passes, browser journey passes
  (hub-switch isolation, mutation target, local errors, details/history/admin roles).
- Ruff clean for all files touched this session; `git diff --check` clean.
- No customer databases, production services or account permissions changed.

### Review against the primary goals

| Goal | Evidence / limits |
|---|---|
| Builder UX | One registered deployment config, visible invalid configuration, migration decision preview, retryable rollback, portal browser journey. |
| End-user UX | Restored model visibility, preserved login cookies, permission parity; live representative-user rehearsal still required. |
| Thin layer | Existing SQLAlchemy transactions/Casbin, OWUI REST, DBOS IDs/history retained; no new dependency or parallel account/scheduler system. |
| Enterprise quality | Atomic rejection, scope checks, regression tests, documented partial-failure recovery; production DB acceptance remains open. |
| Multi-hub default | Registered hub/model checks, per-hub DBOS, scope isolation tests; two processes tested for one initialized SQLite hub. |
| Scheduling insights | Errors and missing heartbeat visible; missed slots remain reports, not replay. |

### Remaining work / cautions

1. Rehearse migration and rollback on a clone of a real supported OWUI deployment,
   including pending/deleted/re-created users, owners/admins and mixed legacy hubs.
   Email recycling is not solved by these changes; identity remains email keyed.
2. CSV-only migration cannot independently establish OWUI model-entry semantics;
   its explicit warning is not equivalent to a verified before/after model matrix.
3. Validate production PostgreSQL concurrent migration/admin-revoke behavior and
   DBOS restart recovery. No live PostgreSQL service was used in these sessions.
4. Full deployment-env lifecycle (gateway start, per-hub dotenv, first-bridge sync
   service credentials) still merits a launch-level acceptance test; unit checks
   cover conflicts but do not prove every operator configuration.
5. Optional maintainability pass on dense TSX/Python from original implementation.
   Avoid unrelated formatting churn or adding a frontend framework.
6. Keep the short maintenance window for migration/rollback. Snapshots and OWUI
   source reads are not one transaction across independent databases. Stop the
   projection worker before restoring legacy ACLs, as documented.

New tests this session: `tests/test_access_safety.py`,
`tests/test_workflow_processes.py`, added cases in `test_edge.py` and
`test_workflow_observability.py`.

### Final validation and additional findings

- **Final complete model-free suite: 1,418 passed, 4 skipped, 21 deselected.**
  Loopback-enabled browser and edge integration tests ran. Remaining warnings are
  dependency deprecations, not failed assertions.
- An intermediate final run exposed an actual intermittent schema-cache bug:
  `id(engine)` could be reused after candidate migration engines were collected,
  causing access-table creation to be skipped. Access and workflow-state schema
  caches now use `WeakSet[Engine]`; regression checks engine lifetime and a fresh
  database. Full suite above is after this fix.
- Visual inspection of mobile screenshot exposed selected permission text white
  on white due to conflicting `bg-panel`/`bg-accent` classes. Fixed with existing
  `cn`/tailwind-merge utility; added browser computed-color assertion. No new
  dependency. Portal lint and build pass after this change.
- Current packaged assets: `index-HED75VMu.js`, `index-OtXofGNi.css`; do not retain
  old hashed artifacts when staging eventually. Nothing staged or committed here.

---

## Session 4 handover — 2026-09-17 (read this section first)

User asked to complete every remaining item and report whether the entire plan is
done, then explicitly requested a continuation plan before credits expire.
**Do not push or commit. Working tree is intentionally uncommitted.**

### Completed in this session

1. **Real PostgreSQL acceptance:** added `tests/test_postgres_acceptance.py`. Starts
   its own temporary UTF-8 PostgreSQL 15 cluster on a random loopback port and
   stops it in fixture cleanup; never uses any existing database. Five tests
   passed together: concurrent last-admin removal, cross-instance policy refresh,
   independent hub migrations/rollback, same-hub concurrent replacement, timestamp
   precision, and actual DBOS process-kill/restart recovery (five test functions;
   some cover multiple assertions). A completed DBOS step ran once after recovery.
2. **PostgreSQL fixes discovered:** migration/restore take transaction-scoped
   per-hub advisory locks so replacement plans cannot merge on an empty hub;
   schema init has its own advisory lock. Audit/identity/catalog timestamps use
   DOUBLE PRECISION; existing PostgreSQL REAL columns upgraded on initialization.
3. **Actual OWUI rehearsal:** `tests/test_owui_rehearsal.py` uses installed OWUI's
   real Alembic migrations and async Users/Groups/Models/AccessGrants APIs in a
   subprocess, synthetic accounts only. Cutover + visibility rollback passed;
   owner/admin, allowed/denied, pending fixture and untouched other model covered.
4. **Local customer copies:** located two local customer OWUI databases, opened
   read-only, copied with SQLite backup into a temporary directory, rehearsed
   explicit standalone-public migration and rollback there. Both passed, two
   effective decisions each, no conflicts, no source modifications. Both sources
   contain one user and zero custom models/groups, so they do NOT substitute for
   rich multi-user acceptance (the real-OWUI synthetic test provides that).
   `/tmp/hz_customer_rehearsal.py` holds the scratch script; it is not a repo tool
   and copies are removed on completion. Do not expose customer identifiers/data.
5. **CSV-only safety resolved:** CLI `--apply` refuses plans with no independent
   expected decisions. `--standalone-public` explicitly asserts legacy public
   signed-in entry and compares tools against the actual legacy CSV resolver.
   If local OWUI DB exists it also imports its tool groups; `--from-owui` can point
   to a copy. Gateways reject standalone bypass. `access diff` has matching flag.
   CSV BOM/case/space header normalization added; repeated emails union groups,
   only inconsistent center attributes conflict. OWUI reader now supports mixed
   schema generations (group_member with model.access_control).
6. **Account lifecycle:** migration binds verified OWUI IDs atomically. Pending
   accounts retain intended grants but cannot enter until approved. Complete
   directory refresh marks previously bound missing accounts unavailable while
   preserving signup grants. Changed account ID for the same email removes old
   direct grants, blocks access and audits replacement; manual review/reactivation
   is required. Chat trusted user-ID header, portal session and MCP API-key lookup
   bind IDs, so replacement is checked on use, not just the sync timer. Migration
   refuses a conflicting existing binding. API-key lookup failures deny.
7. **Configuration lifecycle:** gateway plan restores the original environment
   after loading EACH hub, eliminating cross-hub dotenv/secret contamination.
   `tests/test_deployment_lifecycle.py` drives real gateway CLI wiring with fake
   OWUI/edge processes but REAL child-process settings/manifest reads, then a
   separate operator process: shared op DB, distinct DBOS DBs, same OWUI URL,
   service creds available, first-hub private secret absent from second child.
8. **Maintainability:** formatted dense portal TSX/API/browser journey with pinned
   temporary Prettier 3.6.2 (no dependency added). Black formatted the new/reworked
   access/portal/workflow modules and acceptance tests; recent calls explicitly
   target py311. Scoped Ruff passes. Docs administration updated for new migration
   flag, account lifecycle and env isolation.

### Validation already completed this session

- PostgreSQL acceptance: **5 passed**, actual temporary cluster and real DBOS.
- Migration/portal/access/real-OWUI/gateway/lifecycle affected set: **78 passed**
  (before latest mixed-schema/standalone additions).
- Latest smaller access/API-key/CLI set: **28 passed**.
- Migration/CLI/real-OWUI set: **15 passed** after standalone source changes.
- Two local customer copies: **cutover/rollback passed**, limited schemas above.
- `ruff check` on access package, deployment, portal, gateway and new tests passed.
- Prior session full suite was **1418 passed, 4 skipped, 21 deselected**. This is
  NOT the final count for session 4; final full regression remains to be run.

### Exact next steps — do not claim final completion until these pass

1. Run the full model-free suite WITH loopback approval (PostgreSQL fixture also
   requires process/socket permission):
   `/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python -m pytest -q -m 'not e2e'`
   Tools `exec_command` use `sandbox_permissions=require_escalated` with reason
   temporary PostgreSQL + local browser/proxy tests. Earlier approval accepted.
   Address any failures; recent changes most likely affect expected migration
   plan/public grants, account pending states or env inheritance assumptions.
2. Run portal lint, build, browser journey (browser requires approved loopback):
   `npm --prefix portal run lint`; `npm --prefix portal run build`;
   `npm --prefix portal test`. Rebuild is REQUIRED after formatting; packaged
   assets still have pre-format hashes until build. Vite cleans old assets.
3. Run scoped Ruff including every changed Python file and `git diff --check`.
   Seven pre-existing Ruff errors outside branch changes existed previously;
   don't churn unrelated files. Ensure no temporary npm package/lock added.
4. Review docs accuracy: account replacement last-admin recovery is local
   `access bootstrap --admin <new-verified-email>`. Pending account grants are
   preserved but unavailable. Rollback still requires pausing access edits and
   visibility worker; no distributed transaction across OWUI and access store.
5. Write a concise final completion matrix (nine user goal areas / twelve findings)
   in `docs/AUTH-SCHEDULES-VERIFICATION.md`, with concrete tests and final numbers.
   Label deployment boundaries clearly: SQLite one bridge per hub; PostgreSQL
   concurrency/recovery now tested; no live production cutover performed; external
   provider/real-LLM tests are excluded because this work is model-free.
6. Final user answer: whether implementation + acceptance plan is complete,
   any actual remaining blockers (not generic hypotheticals), link verification
   and this handover. No commit/push. Don't call a production rollout completed.

### Working state / caution

- All current edits are shared in `/Users/shreyarao/Desktop/WaveAssist/Hubzoid/HubZoid`,
  branch `auth-schedules-upgrade`. Do not reset anything.
- No known live test subprocesses remain. PostgreSQL fixture stops its own cluster
  in finally. No external/customer writes were made.
- Tests added this session are untracked until an eventual authorized commit.
- Original AGENTS.md says thin layer, runtime neutrality; no new framework added.
- No subagents were used or authorized.

## URGENT FINAL HANDOVER — latest state, 2026-09-17

User explicitly requested an urgent handover now. This supersedes earlier
session-4 “exact next steps” where results are listed below. No commit/push.

### Completed validation since the previous handover

- Full model-free suite with loopback permission: **1434 passed, 4 skipped,
  21 deselected**, 105.94 seconds. This includes the real OWUI rehearsal,
  real child-process gateway lifecycle and five PostgreSQL acceptance tests.
- Added sixth PostgreSQL test AFTER that collection: same-named workflows in
  `team.alpha` and `team-alpha` share one PostgreSQL DB but return/read only
  their own outputs. **All 6 PostgreSQL tests passed**, 25.75 seconds.
- Portal lint/build/browser journey all pass after formatting. Current packaged
  assets are `index-BKB_gtvc.js` and `index-OtXofGNi.css`.
- Scoped Ruff found five old unused-import/style issues in our untracked
  `tests/test_admin_journey.py`; fixed with Ruff. No behavior change.
- Created **docs/AUTH-SCHEDULES-VERIFICATION.md** with the nine-goal matrix,
  original twelve findings, evidence and honest rollout boundaries. It still
  needs the final validation counts / completion status after the latest fixes.

### TWO FINAL SAFEGUARDS added after the full suite — verify next

1. `hubzoid/access/owui.py::users`: strict complete directory pagination validation.
   Requires users list and nonnegative integer total, stable total across pages,
   valid unique IDs/emails, no missing page. Prevents malformed/partial responses
   from being interpreted as mass account deletion. Four parameterized regression
   cases appended to `tests/test_access_safety.py`. Black formatted both files.
2. **Duplicate hub access domains**: found that artifact slugs deduplicate equal
   folder names, but Casbin domains still use folder names. To prevent cross-hub
   access leakage, gateway CLI now refuses duplicate case-insensitive directory
   names BEFORE startup or writing the manifest. `deployment.py` validates unique,
   nonempty, non-reserved keys on manifest read/save. Test appended to
   `tests/test_deployment_lifecycle.py` asserting no manifest is written.
   This is intentional fail-safe behavior; document the unique folder-name
   requirement in ADMINISTRATION.md. Do NOT change the pure gateway.plan slug
   dedup test: plan artifact routing is still valid; CLI/deployment access domain
   registration is where ambiguity is refused.

### Running tests at handover

- exec session **42178**: focused access-safety/admin-journey/portal tests after
  directory hardening; poll with write_stdin if available.
- exec session **24481**: focused deployment-lifecycle/admin-journey/access-safety
  tests after duplicate-domain guard; poll with write_stdin if available.
- No other running test processes known; prior full suite, PostgreSQL clusters
  and browser journeys finished/cleaned up. These focused tests use temporary
  files; no production/customer changes.

### Concrete continuation checklist

1. Read the two focused test results above. Fix failures, if any.
2. Format final deployment changes (`black --target-version py311` on deployment.py
   and test_deployment_lifecycle.py; CLI has dense pre-existing code, avoid entire
   unrelated churn). Run Ruff on all changed/new Python files, `git diff --check`.
3. Add unique hub-folder/access-domain rule and strict directory-response behavior
   to ADMINISTRATION.md; add these findings to verification matrix as applicable.
4. Run full model-free suite one final time with escalation for local sockets:
   `/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python -m pytest -q -m 'not e2e'`
   Expected prior 1434 + 1 PostgreSQL + 4 directory + 1 duplicate-domain = **1440**
   passes if no other collection changes. Treat ACTUAL output as authority.
   Temporary PostgreSQL initdb/pg_ctl auto-detected; fixture uses UTF8, loopback,
   Unix sockets disabled, stops cluster in finally. Missing binaries would skip,
   but they are installed here and were successfully exercised.
5. No frontend changes since passing lint/build/browser; don't repeat without
   reason. Build assets are current. Check no package-lock/dependency was added
   by temporary Prettier (npm exec, not project install).
6. Update docs/AUTH-SCHEDULES-VERIFICATION.md and this file with actual final
   result. Final user response should explicitly say whether ALL implementation
   and local acceptance items are complete, plus boundaries: no live production
   rollout, real-LLM/provider tests excluded, SQLite one bridge per hub.
7. No commits or pushes without user instruction.

### Where to resume

Repo: `/Users/shreyarao/Desktop/WaveAssist/Hubzoid/HubZoid`
Branch: `auth-schedules-upgrade`.
Python: `/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python`.
All prior edits preserved in the working tree; many new source/tests/docs are
untracked. Do not reset or discard. Latest user request is urgent handover;
original objective remains completing all reviewed findings and reporting status.

---

## Final close-out — 2026-09-17 (the two safeguards are now verified)

The continuation checklist above is complete. No commit, no push.

- **The two late safeguards verified green.** Ran the focused
  access-safety/deployment-lifecycle/observability/admin-journey set: one failure,
  and it was a *test-only* brittleness — `test_duplicate_access_domains_refused...`
  matched an exact substring that rich/click line-wraps ("domains \ncannot
  overlap"). The guard message and behavior were correct; fixed the assertion to
  normalize whitespace. No production code changed to pass a test.
- **Full model-free suite: 1,440 passed, 4 skipped, 21 deselected** (88.5s) with
  loopback approval — real-OWUI rehearsal, child-process gateway lifecycle, six
  PostgreSQL acceptance tests, two-process DBOS dispatch, browser/edge proxy all
  included. Matches the predicted 1,440.
- Portal lint warning-free; build passes; browser journey passes. Assets
  `index-BKB_gtvc.js` / `index-OtXofGNi.css` are consistent with `index.html`.
- `black` on the final deployment files; `ruff check` clean on all changed/new
  Python; `git diff --check` clean; no npm dependency/lockfile added.
- Docs: added the unique hub-folder / access-domain rule to
  `docs/ADMINISTRATION.md`; recorded final counts and completion status in
  `docs/AUTH-SCHEDULES-VERIFICATION.md`.

**Implementation + local acceptance are complete and green.** Genuine remaining
work is deployment-boundary, not code: a live production cutover/rollback rehearsal
on a real supported OWUI+PostgreSQL deployment (with pending/deleted/re-created
users and mixed legacy hubs), and PostgreSQL HA behavior under real production
load. SQLite stays one-bridge-per-hub. Real-LLM/provider tests remain out of scope
(this work is model-free). Still uncommitted and unpushed by request.
