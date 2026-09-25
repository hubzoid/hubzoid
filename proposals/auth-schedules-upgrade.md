# Auth + Schedules Upgrade

Full design + rationale live in the sibling planning folder
`../AuthSchedulesUpgrade/` (`FINAL-PLAN.html`, `ACCESS-SOURCE-OF-TRUTH.md`,
`docs/implementation-plan.html`, `CONTEXT-AND-RATIONALE.md`). This proposal is
the in-repo build contract; where wording conflicts, `ACCESS-SOURCE-OF-TRUTH.md`
wins for access and `implementation-plan.html` for runtime + portal.

## Problem
Three gaps today, all observed on a production multi-hub deployment:
1. **No scheduled deterministic work.** The only scheduler runs markdown *agent
   tasks* (`schedule/*.md`) through the LLM harness. There is no way to run a
   plain, durable, code-defined job (e.g. "review new PRs every 2 min") that
   survives a restart without re-calling the model.
2. **Access is a flat group union with no per-hub scope, no hub-entry gate, and
   no single authority.** `access/policy.py` checks `permission in
   identity.groups`, where groups are unioned across OWUI + roster + header
   (`groups.py`). Hub entry lives in a different place (OWUI model ACL +
   `MCP_ACCESS_GROUP`), so "I granted the tool but they can't see the hub" is a
   standing support ticket, and there is no one place to answer "what can this
   person do here?".
3. **No admin surface.** Access is edited in OWUI's UI (which can't express
   per-hub Hubzoid permissions) or by hand-editing `access.csv`.

## Why now
GitZoid is the first real deterministic workflow we want to run *on* Hubzoid,
and one production deployment has ~9 hubs with real per-hub restricted-function
permissions that OWUI groups model badly. Both land together or not at all: a
workflow's service identity is granted through the same access system.

## What should happen
One release, no phases. Three things, thin glue only:

1. **Workflows.** `from hubzoid import workflow, step, hub`. A `@workflow`
   coordinates steps, calls agents, retains state, retries, and resumes after a
   restart — durable execution on embedded **DBOS** (SQLite default, Postgres
   via `DATABASE_URL`), the engine invisible behind the façade. Workflows live
   in `workflows/<name>/`, run as a service identity `workflow:<name>`, and are
   scheduled by a per-minute in-zone dispatcher (croniter). At-least-once, not
   exactly-once; secrets resolved inside steps; concurrency 1 per hub; started
   by `hubzoid gateway` and by `hubzoid run` only when `HUBZOID_SCHEDULES=1`.
2. **Access on Casbin, direct grants only.** One store (`hz_grants`), grants are
   `(subject, permission, hub)`, behind `can(subject, hub, action)`. Two
   permissions per hub: `use_hub` (entry) + the restricted-function stems, with
   an implication rule (grant any tool perm ⇒ `use_hub`; revoke `use_hub` ⇒
   revoke the hub's perms). The existing **surface gate stays in front** of
   `can()`; a new `workflow` surface lets a service identity reach a granted
   tool. One transactional `grant_service` (grant + implication + last-admin
   guard + audit + `policy_revision` + a projection outbox) that the portal,
   CLI, CSV import, migration, and first-login all call. Every grantee is a row
   in `hz_identities`; that row id is the Casbin subject (not the email).
   Attributes (`center`) are keyed `(hub, subject, key)`. Delegated admin via a
   reserved org domain (`manage_access`), scope-bounded not holdings-bounded.
3. **Portal.** A React (Vite + Tailwind + shadcn) SPA built static and served by
   the existing FastAPI. Five screens (Overview, Workflows, Access, Permissions,
   Audit), **view-only except Access**. Same OWUI/OIDC session as chat, on new
   `edge.py` routes that strip inbound identity headers and validate the session
   via OWUI `GET /api/v1/auths/`; entry gated by `can(user, *, manage_access)`.

Plus: one-time migration (OWUI-0.11 `access_grant` adapter that flattens groups
to direct grants, refuses unknown schema, preflight-fails on dynamic authz
rosters), validated on a clone then a short freeze + atomic cutover; a one-way
Casbin→OWUI visibility reconciler so users see only their hubs; the OWUI
access-UI lock derived from the migration completion marker.

## Scope and non-goals
- **Out of scope (not phases):** live IdP group→permission mapping, a
  management/write API, IdP directory-import connectors, role bundles,
  per-workflow concurrency overrides.
- Does **not** fix the scheduled git-push isolation bug (separate).
- The markdown agent-task scheduler is unchanged (keeps its behaviour and its
  `HUBZOID_DISABLE_SCHEDULE` gate); workflows are a second, side-by-side source.

## Open questions
- Gateway DBOS topology (N bridge processes over one SQLite system DB) is a
  named build gate: prove a two-bridge recovery/scheduling test; fallback is
  per-bridge DBOS system tables (operational tables still shared) or Postgres.

## Definition of done
- `can()` is the one authority every surface consults; `access/policy.py` reads
  Casbin, surface gate intact; unit tests for grant/revoke, `use_hub`
  implication, last-admin guard, wildcard subject, org domain, `policy_revision`.
- A workflow in `HubzoidTestHub` runs twice and the second run skips completed
  steps (durable state); schedule fires in-zone; service identity is enforced.
- Migration on a clone of a real OWUI-0.11 hub: replay + full static diff both
  zero, before any cutover.
- Portal builds static, loads behind the bridge, shows the 5 screens, edits only
  Access, gated by `can(*, manage_access)`.
- `pytest` green; new loaders/tools have unit tests; e2e auto-skip without keys.

## Accepted UX completion work (September 2026)

Implement the reviewed twelve gaps: multi-hub data isolation, safe portal state,
OWUI 0.11 migration and rollback, deployment discovery, visibility projection,
workflow dry-run and observation, schedule validation, complete change history,
OWUI-backed user lifecycle, usable portal interactions, and operator docs/tests.
Keep preview and atomic cutover. Reuse Casbin, OWUI and DBOS; introduce no second
account or execution-history system. Validate two hubs, overlapping workflow
names, org/hub admins and ordinary users. No publishing or pushing is requested.
