# Auth and schedules: acceptance against the product goals

Updated 2026-09-17. Branch `auth-schedules-upgrade`. Changes remain uncommitted;
nothing pushed. This is implementation and local acceptance, not a production
rollout record.

## User journeys and architecture

The builder uses the portal/CLI to configure agents, manage grants, inspect access
changes, and see runs and schedules across hubs. End users retain OWUI accounts,
sessions and chat; agent entry and restricted tools enforce shared Hubzoid access.
OWUI owns accounts, Casbin owns permissions, DBOS owns execution/history. No new
account service, scheduler framework or frontend framework was introduced.

## Goal-by-goal checks

| Goal | Implemented and checked | Evidence |
|---|---|---|
| 1. Consistent, reliable access with history | Single operational store; revision refresh; shared validation for single/bulk/migration; atomic rollback; audited bootstrap/import/replacement/offboarding; PostgreSQL timestamp precision | access store, safety, migration and PostgreSQL tests |
| 2. Clean, deterministic enforcement | Surface gate remains first; verified identity; hub entry and tool decisions share the authority; blocked/replaced accounts denied; malformed imports cannot partially apply | access/guard/MCP/portal tests, recycled-account chat and API-key tests |
| 3. User/admin management | Existing vs pending signup/approval, cross-hub effective access, block/reactivate, delegated scope, org admin protection, removed/re-created account handling | portal/admin journey and directory lifecycle tests; concurrent last-admin PostgreSQL test |
| 4. Minimal-impact migration | Dry-run decision comparison; explicit source requirements; CSV legacy resolver comparison; normalized/mixed OWUI schemas; backup + atomic cutover; API visibility rollback with retryable partial failure | actual OWUI schema/API rehearsal, two local read-only customer copies, migration/rollback tests |
| 5. Enterprise portal UX | Search/pagination, readable labels, local recoverable errors, disabled unavailable actions, stale-hub response rejection, visible selected permissions, desktop/mobile layout inspection | lint/build, Playwright journey, screenshot review; formatted source |
| 6. Coherent OWUI experience | One OWUI account directory, shared manifest, scoped visibility projection, managed-model locks with legacy groups retained, admin-only portal link, sessions/cookies preserved | reconcile, real OWUI model API, proxy compressed HTML/cookie tests |
| 7. Simple observable workflows | Import-free dry-run, explicit unsupported flags, hub-scoped runs/status/output/steps, configuration errors visible | workflow/admin journey tests and real DBOS tests |
| 8. Reliable observable scheduling | Validated schedules/timezones, deterministic scheduled IDs, stale/missing heartbeat, dispatch errors, missed-slot notices, startup/shutdown cleanup | grammar/observability tests, two-process dispatch, PostgreSQL killed-process recovery |
| 9. Multi-hub default | Grant-less hubs listed, selected-hub resolution, scoped audit/history, isolated dotenv/secrets, shared operational DB, distinct SQLite DBOS or shared PostgreSQL | two-hub/three-role acceptance; real child-process configuration lifecycle; PostgreSQL isolation tests |

## Original twelve findings

| Finding | Disposition |
|---|---|
| Multi-hub portal scope/leak | Fixed and covered |
| Stale hub-switch mutation race | Fixed and browser-tested |
| Migration source/effective diff/rollback | Fixed; unverified CSV cutover refused; explicit standalone path and OWUI model path rehearsed |
| DB/auth configuration consistency | Fixed; conflicting URLs rejected; child-process discovery/isolation tested |
| OWUI sync/locks/navigation | Implemented and tested |
| Workflow dry-run executes | Fixed and tested without importing workflow code |
| Workflow visibility/name collisions | Implemented with hub scope and run details |
| Scheduling visibility/dedup | Implemented; real multi-process and PostgreSQL recovery tested |
| Access-change history gaps | Audited writes and portal history implemented |
| User lifecycle/offboarding | Implemented including pending/deleted/re-created account behavior |
| Portal quality gaps | Search/pagination, inline failures, role-aware actions, contrast fix, formatted source |
| Documentation and acceptance coverage | Administration guide, env documentation, root/deployment/access links and expanded acceptance suite |

## Validation boundaries

- Customer-source rehearsals used temporary read-only copies. Both local source
  databases contain one user and no custom models/groups; their explicit
  standalone-public cutover/rollback passed. Rich owner/admin/group/deny cases
  use OWUI's actual migrated schema/APIs with synthetic accounts.
- PostgreSQL testing is against a temporary local PostgreSQL 15 cluster, not a
  production server. It exercises real transactions and DBOS recovery.
- Embedded SQLite supports one bridge per hub. Same-slot duplicate dispatch is
  tested against an initialized DB; simultaneous fresh SQLite schema creation is
  not a supported production HA topology. Use PostgreSQL for production workers.
- Migration/rollback requires freezing access edits and pausing visibility sync.
  There is no distributed transaction spanning OWUI and the operational store.
  A failed OWUI restore is explicit, returns failure, and is retryable.
- External-provider/real-LLM tests are excluded from this model-free acceptance.
  No production deployment, live customer cutover, commit or push was performed.

## Final validation (2026-09-17, after the two late safeguards)

The two safeguards that were added after the previous full suite — strict OWUI
directory-pagination validation (`access/owui.py`) and the duplicate hub
access-domain guard (`gateway` CLI + `deployment.py`) — are now verified:

- **Full model-free suite with loopback approval: 1,440 passed, 4 skipped, 21
  deselected** (88.5s). Includes the real-OWUI rehearsal, the real child-process
  gateway lifecycle test, six PostgreSQL acceptance tests (temporary local
  cluster), the two-process DBOS dispatch test, and the browser/edge proxy tests.
- One brittle assertion in the new duplicate-domain test was fixed (it matched an
  exact substring that the CLI line-wraps); the guard behavior itself was correct.
  No production/behavioral code was changed to make a test pass.
- Portal `oxlint` warning-free; `tsc -b && vite build` passes; browser journey
  passes. Packaged assets `index-BKB_gtvc.js` / `index-OtXofGNi.css` match
  `index.html`.
- `ruff check` clean on every changed/new Python file; `git diff --check` clean;
  no npm dependency or lockfile added (only a `test` script).

**Status: all reviewed findings and the local acceptance plan are implemented and
green.** The boundaries in "Validation boundaries" above still stand: SQLite is one
bridge per hub (PostgreSQL for production HA); migration/rollback needs a short
maintenance window and is not a distributed transaction across OWUI and the
operational store; no live production cutover was performed; real-LLM/provider
tests are excluded from this model-free acceptance. **Nothing committed or pushed.**

## Continuation

The latest run results and any actionable remaining checks are recorded at the
end of [the hand-back](../AUTH-SCHEDULES-HANDBACK.md). Earlier historical “open”
sections in that file are superseded by the session 4 status and final addendum.
