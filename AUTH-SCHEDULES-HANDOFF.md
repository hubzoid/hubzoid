# Resume: auth-schedules-upgrade UX completion

Updated 2026-09-17. **WORK IN PROGRESS — NOT READY TO MERGE.**
This file is a continuation handoff, not a completion report. No implementation
commits or pushes have been made during this work. Preserve the working tree.

## Where to resume

- Repository: `/Users/shreyarao/Desktop/WaveAssist/Hubzoid/HubZoid`
- Current branch: `auth-schedules-upgrade`
- Branch HEAD before these uncommitted fixes: `0c46127`
- It was rebased successfully onto local `main` (`c35dad8`).
- Python: `/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python`
- Environment has DBOS **2.31.1**, OWUI **0.11.0**, pytest, black and ruff.
- Node: v22.23.2. Installed portal dependencies include Playwright.
- Read `AGENTS.md` and this file, inspect `git diff` and `git status`, then continue.
- Do not reset, overwrite, or abandon the uncommitted fixes below.
- Do not push. User previously explicitly said not to push; still applies.
- User authorized implementation of all twelve reviewed areas. Do not ask for
  approval again just because the local implement-proposal skill has a gate.
  The review and explicit "implement and fix all of these" are the authorization.
- No delegation was requested. Current instructions prohibit spawning agents
  without explicit user or applicable skill authorization.

## User's priorities (load-bearing)

1. Builder UX: the person who configures/codes agents and grants end users access.
2. End-user UX: the person signing in and using an agent.
3. Thin integration over existing libraries, low long-term maintenance.
4. Enterprise quality, clear documentation, reliable history and observability.
5. **Multi-hub is the default**, not an optional edge case.
6. Workflow durability is less important than simple execution and useful insights.

The secondary reviewer agreed with the twelve findings below, with two nuances:
preview and atomic cutover already existed; change-history API and revoke_all
already existed but were incompletely surfaced. Preserve those existing pieces.

## Findings we are fixing

1. Portal first-bridge hub leakage: wrong permission catalogs, unscoped audit,
   grant-less hubs missing, overview not deployment-wide.
2. Changing hub leaves old rows clickable and posts their permissions to new hub.
3. Migration uses removed OWUI columns, misses restricted-tool groups, lacks
   effective before/after verification and backup/rollback.
4. CLI can write a different DB than gateway; portal internal OWUI URL not wired.
5. Visibility sync only printed a plan; global group lock breaks partial migration;
   no portal navigation in OWUI.
6. Workflow `--dry-run` actually executes; markdown-only flags silently ignored.
7. No useful workflow runs/results/errors/steps; same-name workflows collide in UI;
   schedule list/status only cover old markdown jobs.
8. Workflow validation and scheduler health/missed-run insight inadequate.
9. Audit incomplete for bootstrap/bulk/migration; existing change-history API absent
   from frontend.
10. Adding a grant looks like adding an account; identities missing on normal grants;
    no clear pending/active/account ownership/offboarding journey.
11. UI lacks scale/search/paging, mutation recovery, appropriate admin controls,
    understandable labels and explicit sensitivity metadata.
12. Missing operator documentation and combined multi-hub acceptance tests.

## Architecture choices already implemented

- `hubzoid/deployment.py`: gateway-owned deployment manifest + per-hub pointers
  under `.hubzoid`. Shared hub catalog includes key/name/path/model_id/DBOS URL.
  One manifest supplies operational DB and internal OWUI URL to portal/CLI/bridges.
  Files written atomically with mode 0600. DB URLs MAY contain credentials;
  correct any comments implying the entire manifest cannot contain credentials.
- `db.operational_url`: registered deployment is authoritative. Conflicting
  `HUBZOID_OPERATIONAL_DB` fails rather than silently splitting access stores.
  `DATABASE_URL` continues to work for unregistered standalone hubs.
- SQLite still has OWUI DB + one shared operational DB + one DBOS DB per hub.
  PostgreSQL can share storage. Do NOT blindly collapse unsupported DBOS SQLite
  topology to satisfy the phrase "single database". Explain one deployment config.
- OWUI remains the account/authentication owner. New API adapter in
  `hubzoid/access/owui.py` reuses existing service-account login and paginated users.
- Casbin remains permission authority; DBOS remains execution-history authority.
  No parallel account or run-history database was introduced.
- Optional `identity/permissions.yaml` provides labels/descriptions/sensitivity.
  Portal discovers restricted file stems without executing restricted Python code.

## Implemented so far (all uncommitted)

### Deployment/configuration

- New deployment manifest module; gateway saves it before launching bridges.
- Gateway captures deployment env before its existing plan() loads per-hub .env.
- Shared operational URL pinned into bridge env; CLI discovers it via hub pointer.
- Gateway config records each hub's DBOS URL; rejects shared explicit SQLite DBOS
  URL when launching multiple hubs.
- Standalone run supplies internal OWUI URL before bridge launch.
- CLI grant/revoke display target DB URL with password hidden.

### Access store/enforcement

- Identity rows created by normal grant insertion, not just migration.
- Bootstrap, bulk import, migration, implied grants and cascaded revokes audited.
- Scoped/paginated change-history query in SQL, not filter-after-limit.
- Snapshot/restore methods for hub-scoped grants/attrs/authority; rollback audited.
- Per-hub false authority markers override old global true marker (rollback).
- CLI bootstrap --authoritative now marks selected hub, not entire deployment.
- Blocking/offboarding removes direct grants and stores a deny marker, including
  public access. Reactivation does not restore old grants. Last org admin protected.
- Portal grants to blocked users refused. OWUI account itself remains unchanged.
- Added blocked-user checks in chat, MCP, and restricted-tool guard (including
  legacy hubs when a verified identity is available).
- Surface gate checked before loading Casbin for restricted tools.
- Enforcer initial revision set stale to avoid old-policy/new-revision race.
- Revision-row initialization now INSERT ... ON CONFLICT DO NOTHING.
- Store grants validate reserved org/public scopes (latest edits need rerun).

### Portal backend

- Substantial rewrite of `portal.py`, preserving session resolver/static mount.
- Hub lookup via registered deployment; unknown hubs/permissions rejected.
- Permissions, access and audit scoped to selected/authorized hub.
- Audit combines authorized hub files and sorts/paginates. Org changes available
  only to org admin; scoped admins cannot see hub-null/global changes.
- Access response includes direct/inherited/effective permissions, public state,
  account status, labels, search and pagination.
- Strict mutation request models and origin checks.
- Scoped admins cannot indirectly revoke admin rights through use_hub cascade.
- People listing, directory refresh (org admin), block/reactivate endpoints.
- Org-admin grants supported via reserved org domain.
- Workflow catalog and DBOS run/detail endpoints are hub-scoped.
- Overview shows managed/legacy counts and visibility-sync health.

### OWUI experience

- Production `sync_owui` in `access/reconcile.py` updates migrated model ACLs via
  OWUI REST, including EMPTY ACL after last revocation, expands public access over
  current non-pending OWUI users, refreshes identity rows, reports sync errors.
- CLI access sync actually executes it and returns failure on errors.
- First gateway bridge runs sync loop every 30 seconds; server cancels on shutdown.
- Dynamic edge lock checks only migrated model ACL writes/deletion. Automatic
  broad group locks removed, so legacy groups and other OWUI resources remain usable.
- Explicit operator group-lock env behavior retained for compatibility.
- New `portal_navigation.py`: edge injects self-hosted JS link into OWUI HTML.
  JS only displays Manage agent access after authenticated /portal/api/me succeeds.
  No installed OWUI bundle modifications. Needs live response-path testing.

### Migration

- Supports modern group_member/access_grant and explicitly detected old JSON schema.
- Preserves model-entry intersection with CSV + OWUI restricted-tool groups.
- Includes model owner/OWUI admin entry; public access handled.
- Builds an expected matrix with allowed AND denied users/actions; candidate store
  checked before cutover using `effective_diff`.
- CSV reserved admin/wildcard permissions become conflicts instead of escalation.
- Existing default preview and atomic apply preserved.
- --apply creates protected pre-cutover access snapshot under .hubzoid/backups.
- `access rollback BACKUP HUB` restores hub grants/attrs/authority.
- Full OWUI visibility rollback is NOT automatic: docs require restoring prior OWUI
  ACL/backup if returning to legacy after visibility was projected. Consider making
  this safer/more integrated before declaring the migration goal complete.

### Workflows

- New `workflows/observe.py`: AST definition inspection, validation, catalog health;
  DBOSClient-based run list/details including output/error/steps. No second history DB.
- Dry-run inspects AST and returns before imports/DBOS. Rejects unsupported workflow
  timeout/max-rounds/model flags.
- Workflow entries added to schedule list/status and doctor.
- Literal schedule/timezone validation, duplicate names and load failures visible.
- Invalid settings YAML now raises instead of silently running with empty settings.
- Minute-boundary polling instead of arbitrary 60-second tick.
- Scheduled IDs deterministic by hub/workflow/slot via DBOS SetWorkflowID.
- Delayed tick skips older slots and counts missed slots; no downtime backfill.
- Workflow health persisted as operational metadata.
- DBOS application names now include hash to avoid collisions from punctuation or
  truncated long names. This LATEST edit happened AFTER the real DBOS test passed;
  rerun. Assess compatibility with any experimental existing DBOS histories.
- Init refuses two hubs in one process instead of silently reusing first hub.

### Frontend

- Rebuilt existing React screens/components, no new UI framework.
- Hub-keyed screens, aborted stale requests, response-bound mutation hub.
- Local mutation errors preserve panel; busy controls, feedback, revoke confirmation.
- Overview, Access, People, Workflows → Runs → Steps, Permissions, Audit.
- Distinguishes accounts awaiting signup/pending approval/services/blocked users.
- Explains grant does not create account or send invitation.
- People block/reactivate and org-admin management controls.
- Search/paging; all-agent option on audit/workflows; hub admin controls disabled.
- Replaced Casbin-facing labels and name-based production guesses.
- Builds successfully; latest generated assets currently tracked as deletes/new names.
  Always rebuild after further TypeScript edits and include all resulting assets.

### Docs/tests

- New `docs/ADMINISTRATION.md`: multi-hub setup, builder/end-user journey, storage,
  migration/rollback, accounts, workflow inspection, semantics, troubleshooting.
- Replaced Vite template README with real portal development guide.
- Appended accepted scope to original proposal.
- New `tests/test_admin_journey.py` (backend acceptance + DBOS test + edge test).
- Existing portal test fixture now declares prod_in permission (API correctly rejects
  nonexistent permissions, so prior synthetic fixture was incomplete).
- New `portal/tests/journey.cjs` + npm test script. Written, NOT YET RUN.

## Validation already performed

These apply to the code at the time of each test; several subsequent edits need rerun.

1. Prior to implementation, branch-specific suite: 82 passed.
2. Early implementation targeted suite: 62 passed, 4 failed because old portal fixture
   didn't declare prod_in. Fixed fixture; subsequent portal suite passed.
3. Acceptance + portal + existing workflow integration: **19 passed**.
4. New real DBOS test: **1 passed**. It enqueues same scheduled slot twice, asserts same
   workflow ID and one side effect, reads SUCCESS/output/steps through DBOSClient.
5. Broad suite during implementation: **1391 passed, 4 skipped, 21 deselected** plus
   2 failures/6 errors, ALL eight caused by sandbox blocking loopback socket binds in
   existing browser/edge tests. Rerun outside sandbox; not an implementation failure.
6. Portal production builds have passed repeatedly, including latest UI revision.
7. Portal lint runs but has one warning: react(set-state-in-effect) in useData hook.
   Needs cleanup; do not claim warning-free lint yet.
8. Latest changes (reserved scopes, more audit events, blocking legacy enforcement,
   app-name hashing, dynamic edge test, more account UI) NOT fully regression-tested.
9. No real customer data modified and no customer migration performed. DB/API tests
   use temporary data. OWUI 0.11 schema verified against installed package source.

## Exact next steps (do not skip)

1. Run new browser test and inspect desktop/mobile screenshots. It uses mocked API
   responses, not a running customer server. It may expose mock/selector issues as
   well as real UI defects; fix without weakening the stale-hub assertions.
2. Test edge HTML injection and cookie/header preservation with a real/mocked HTTP
   upstream. Current added edge test checks lock and JS endpoint but not full injection.
3. Finish scheduling observability:
   - health has heartbeat but UI/catalog do not yet classify stale/stopped dispatchers;
   - missed counter covers delayed ticks, NOT downtime at restart;
   - startup does not persist a downtime window/notice;
   - Dispatcher.stop cancels ticker only, not DBOS worker lifecycle. Inspect supported
     DBOS.destroy(workflow_completion_timeout_sec=...) and implement clean shutdown
     without killing recoverable runs or hanging the bridge;
   - test two actual processes submitting same slot, not just two calls in one process.
4. Review migration edge cases and rollback:
   - CSV-only path still lacks independent effective baseline beyond plan;
   - unknown/disabled OWUI models, owner/admin semantics, reserved CSV groups;
   - snapshot schema validation and correct deployment scope;
   - snapshot timing vs concurrent edits; documented short freeze is still required;
   - explicitly surface the expected effective matrix/differences in CLI preview;
   - OWUI ACL restoration for rollback to legacy is currently a documented manual step.
5. Complete configuration cleanup:
   - per-hub env overrides can still affect DBOS URL/OWUI URL; validate precedence;
   - update db.py/deployment.py comments to match manifest-authoritative behavior;
   - no bootstrap can silently make existing customers managed globally;
   - service credentials must be available to first bridge and CLI sync;
   - fail clearly rather than silently selecting a different DB.
6. Inspect remaining API/store security/consistency:
   - policy_revision refresh under concurrency;
   - bulk/import paths and reserved grant validation consistent with single grant;
   - accurate audit history for restore/revoke_all/suspend/authority changes;
   - verify unknown hub/permission, malformed mutation, last-admin race behavior;
   - blocked users should not keep access via legacy chat/MCP/tool routes;
   - ordinary user's account deletion/disable in OWUI vs stale grants.
7. Finish public docs integration: link guide from root README, docs/DEPLOYING.md and
   docs/access-management.md; document new env vars in settings.py; update stale
   module docstrings and proposal claims.
8. Code quality pass. New Python and TS currently contain dense generated formatting.
   Run targeted black/ruff format on changed/new Python, fix unused imports and warnings.
   Avoid broad unrelated formatting churn. Improve TS formatting with an available
   formatter (do not add a new runtime framework). Resolve useData lint warning while
   retaining stale-data protection.
9. Run complete pytest (with loopback permissions), portal lint/build/browser tests,
   representative doctor on a temporary hub. Fix failures. Recheck git diff --check.
10. Summarize actual completed outcomes/tests and any real remaining limitations.
    Do not claim complete while the outstanding points above remain unresolved.
    Do not push; user has not asked for a new commit either.

## Commands

```bash
cd /Users/shreyarao/Desktop/WaveAssist/Hubzoid/HubZoid
/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python -m pytest -q tests/test_admin_journey.py tests/test_portal_api.py tests/test_access_store.py tests/test_migrate.py tests/test_workflow_integration.py
/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python -m pytest -q -m 'not e2e'
npm --prefix portal run lint
npm --prefix portal run build
npm --prefix portal test
git diff --check
git status --short
```

Use exec escalation for pytest tests that bind local sockets and Playwright browser
launch if sandbox blocks them. Prior auto-review allowed read-only/local test runs.
Do NOT change tests just to suppress sandbox permission failures.

Installed upstream source for API checks (no need to guess API shapes):

- `/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/lib/python3.12/site-packages/dbos/_client.py`
- `.../dbos/_dbos.py`, `.../dbos/_sys_db.py`
- `.../open_webui/models/groups.py`, `models.py`, `access_grants.py`
- `.../open_webui/routers/users.py`

DBOSClient exposes list_workflows(application_name, name, workflow_ids, offset,
limit, load_output, ...), list_workflow_steps, destroy. DBOS.destroy signature is
`destroy(*, destroy_registry=False, workflow_completion_timeout_sec=0)`.

## Practical cautions for the next agent

- This is substantial unfinished work, not twelve fully checked fixes yet.
- Keep scope centered on the two user journeys and multi-hub defaults.
- Do not replace OWUI accounts or DBOS execution records with custom equivalents.
- The `.hubzoid` manifest is optional generated local state; don't make new source
  files mandatory in existing customer hubs.
- Latest user specifically requested this handoff because credits may run out.
  The original implementation goal remains active; this file makes resumption safe.
