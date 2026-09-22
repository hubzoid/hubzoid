# Portal UX redesign — continuation plan

## Goal and decisions
User approved replacing the confusing administration experience with an agent-first interface using Ant Design. Prioritize builder tasks, clear end-user access, multiple agents, and low maintenance. Preserve OWUI identity, Casbin authorization, and DBOS execution. No push or commit requested.

## Existing work to preserve (backend handback history)
Branch `auth-schedules-upgrade`. Pre-existing dirty files that must stay as they are: `hubzoid/access/owui.py` (adds `configured()` so a bridge with no OWUI does not start the visibility loop), `hubzoid/server.py` (uses it). Earlier backend implementation is committed at 2dc921a. `AUTH-SCHEDULES-HANDBACK.md` documents backend work, not UX acceptance. Portal source, tests, docs and the generated `hubzoid/portal_dist` are owned by this redesign.

## Backend semantics the UI must respect (read from `hubzoid/portal.py`, `hubzoid/access/store.py`)
- One permission per request: `POST /portal/api/access/grant|revoke {subject, hub, permission}`. There is no batch endpoint; a multi-change save is **not atomic**.
- Granting any tool capability auto-grants `use_hub` in the same hub (`GrantStore.grant`). Revoking `use_hub` cascades and deletes every direct grant in that hub (`GrantStore.revoke`). Both are audited per row.
- Inherited access: `manage_access` in the org domain `*` appears in `row.inherited`; public entry is the `*` subject with `use_hub`, surfaced as `access.public` and in `row.effective` (not `row.perms`).
- Non-org admins (`can_manage_admins=false`) cannot change `manage_access`, cannot revoke `use_hub` from someone who effectively has `manage_access`, and cannot touch the `*` subject (backend 403s mirror the UI locks).
- Granting to a blocked subject returns 409 "Reactivate this user before granting access". Removing the last org admin (revoke/suspend) returns 409 `LastAdminError`.
- `/access` rows carry `status`: `active | awaiting-signup | pending-approval | blocked | service | everyone`. `/people` rows carry `owui_id`, `pending`, `blocked`, `organization_admin`, `access{hub: perms}`.
- Audit sources: access changes (`hz_access_audit`: actions `grant, revoke, revoke_all, suspend, reactivate, bootstrap, activate, rollback, restore_grant, replace_hub_grants, account_replaced, account_unavailable`; actor `owui-identity` = account sync) and tool decisions (JSONL: `decision allow|deny`, reasons `grant, no-grant, group, no-group, anonymous, surface:<s>, blocked, store-error, unrestricted`).
- Visibility sync (`/overview.visibility.state`): `not-run | legacy | ok | error`; `POST /sync` retries (org admin only). With the dirty `server.py`, a bridge without OWUI never runs it (stays `not-run`).
- Workflows: `state` is `scheduled | manual | disabled | stale | error`; `downtime {since, until, missed}`; runs come from DBOS with `steps` only when a `run_id` is requested.

## Full plan and status
| # | Step | Status |
|---|---|---|
| 1 | Restructure the portal source: `hooks/`, `lib/format.ts`, `components/`, `screens/`. Delete the old Tailwind shell. | ✅ done (old shell gone; antd in; screens lazy-loaded) |
| 2 | Shell + hash routing + recovery screens. | ✅ done (verified by deep-link/recovery journey) |
| 3 | Agent → Access staged editor (review before save, locks, cascade, partial-failure, discard guard). | ✅ done (verified by 13 access journeys) |
| 4 | Agents landing: attention panel + searchable cards. | ✅ done (screenshot `ux_agents.png`) |
| 5 | People: status distinctions, per-agent access, details drawer, block/reactivate/org-admin. | ✅ done (screenshot `ux_people.png`) |
| 6 | Activity: readable sentences + filters. | ✅ done (screenshot `ux_activity.png`; wildcard `*`→"Everyone signed in" fixed) |
| 7 | Runs & schedules: state explanations, alerts, run detail with steps. | ✅ done (verified by runs journey) |
| 8 | Tests: journey around shared fixture + fixture-server. | ✅ done — **21 journeys PASS** |
| 9 | Bundle: lazy-load + vendor split. | ✅ done (antd chunk 846 kB / 270 kB gzip, isolated; screens split) |
| 10 | Docs: `portal/README.md`, `docs/ADMINISTRATION.md` nav names; this handover. | ⏳ handover updated; README/ADMINISTRATION nav-name refresh still pending |

## Checkpoint — 2026-09-18 (session took over from a concurrent build)
A second Claude session's `claude -p` subagent (PID 57166) plus its fixture/journey
servers were still editing these files; with the user's approval I stopped them and
took sole ownership of the tree. Then:

**Fixes made this session (all product-preserving or genuine bugs):**
- `RunsScreen.tsx`: manual-workflow schedule cell now shows "—" instead of a
  redundant "No schedule" (the State column already says "Manual · No schedule…").
- `ActivityScreen.tsx`: `ctx.people` now falls back to `personName(subject)`, so the
  `*` wildcard reads **"Everyone signed in"** in sentences (was rendering raw "*") and
  unknown subjects render sensibly. **Genuine UX bug fixed.**
- `tests/journey.cjs`: fixed brittle/ambiguous locators (exact-match "No schedule"/
  "allowed"; antd Segmented label click; Details/Edit are links not buttons; agent tab
  vs sidebar "Activity"); added `page.reload()` after each role switch (a role change is
  a fresh login, so `/me` + `/hubs` must refetch — this proved hub-admin scoping works);
  restored Aisha's org-admin grant before the hub-admin section (an earlier step had
  stripped all admins but Priya); the console-error collector now ignores the browser's
  automatic "Failed to load resource" logs for the intentionally-provoked 4xx/5xx
  negative-path responses (the app handles them in-UI, asserted separately).

**Validation:**
- `npm run lint` clean, `npm run build` (tsc + vite) passes.
- `npm test` → **PASS: 21 journeys** (agents list, access read, hub-switch isolation,
  edit/cancel/discard-guard, review-save order, cascade remove, add+validate, add-existing,
  blocked-grant refusal, mid-save partial failure + reload, dirty-draft leave guard, no-nav
  during save, public toggle, runs+run detail+steps, activity sentences+filters, people
  states+drawer+admin actions, last-admin protection, route/agent recovery, hub-admin
  scoping+locks, mobile layout, ordinary-user denial). No JS/console errors.
- Visual inspection via fixture server (realistic data), screenshots in scratchpad:
  `ux_agents.png`, `ux_access.png`, `ux_editor.png`, `ux_runs.png`, `ux_activity.png`,
  `ux_people.png`. Quality now matches the goal (agent-first, task-oriented, plain language).

**Live for inspection:** `node portal/tests/fixture-server.cjs 8792` →
http://127.0.0.1:8792/portal/ (synthetic data, org admin by default; role switch via
`/__fixture/role/{org|hub|user}`). Not the real bridge; no live data.

**Still pending (after step 8):**
- Step 10 docs: refresh nav names/screens in `docs/ADMINISTRATION.md` and `portal/README.md`.
- Decide whether to add a backend `POST /access/apply` (atomic multi-change) to remove the
  non-atomic partial-save caveat (currently handled honestly in the UI).

## Checkpoint — brand skin + light/dark (2026-09-18)
User: "what the hell is this branding" — the portal shipped with Ant's default blue
(`#3659d9`) and a placeholder "h" tile, off-brand vs `HubzoidStudio/` (locked V1:
Signal Orange `#E5572A`, Inter + JetBrains Mono, 1px borders, **no shadows**). Kept the
loved 3-section structure/UX; reskinned only. Stayed on Ant Design (theming via tokens) —
did NOT switch to shadcn: that would discard the tested UI and fight the thin-layer
doctrine; the brand is fully expressible through ConfigProvider tokens + a small CSS layer.

**Done:**
- `src/lib/theme.ts`: brand light + dark antd themes (Signal Orange primary/link, Inter,
  JetBrains Mono code, 8px/6px radii, shadows off) + `useThemeMode` (light/dark/system,
  persisted in `localStorage` as `hz-theme`, sets `data-theme` on `<html>`, follows system).
- `Portal.tsx`: picks light/dark theme; passes mode down.
- `components/Shell.tsx`: real `/hubzoid` wordmark (light/dark PNG from the design system),
  a footer theme toggle (sun/moon/monitor Segmented).
- `portal.css`: rewritten around brand CSS variables that flip on `[data-theme="dark"]`;
  self-hosts JetBrains Mono; imports Inter; removes all shadows; neutral (mono) avatars via
  `.hz-avatar` (was blue); `--accent` for the public "everyone" avatar.
- `components/common.tsx`: `AgentAvatar`/`PersonAvatar` now use `.hz-avatar` classes (no blue).
- `screens/AgentsScreen.tsx` + CSS: fixed the card-action placement (Image #5) — primary
  "Manage access" full-width, "Runs & schedules" + "Activity" share the row below (was an
  awkward 3-button wrap).
- Brand assets copied to `portal/src/assets/brand/` (wordmark-light/dark.png, avatar PNGs,
  JetBrainsMono-{Medium,SemiBold}.ttf), bundled by Vite.

**Validation:** lint clean, build passes, **21 journeys still PASS**. Visual proof (fixture
server): `brand_agents_light.png`, `brand_agents_dark.png`, `brand_editor_dark.png` in the
scratchpad. Both themes verified in Playwright.

**Live:** `node portal/tests/fixture-server.cjs 8792` → http://127.0.0.1:8792/portal/
(theme toggle in the sidebar footer).

**Still pending:** docs step 10; optional `POST /access/apply`; wordmark light/dark mapping
was assumed by filename convention (verified visually — correct). Nothing committed or pushed.

## Verification / exact next steps
1. Implement steps 1–3 (shell, routing, Access editor), run `npm run lint && npm run build`.
2. Implement 4–7, then 8 (tests) and 9 (bundle), run `npm test`.
3. Visual inspection at desktop (1440×950) and mobile (390×844) through the fixture server; screenshots under `/private/tmp/hubzoid-portal-*.png`.
4. Update docs, final handover.

Commands: from `portal/`: `npm run lint`, `npm run build` (writes `hubzoid/portal_dist`), `npm test`. Python: `/Users/shreyarao/Desktop/WaveAssist/waveAssistEnv/bin/python`.

## Live server on 8790 (read-only investigation)
PID 47271 (`python -m uvicorn hubzoid.server:build_app --factory --port 8790`), child of `hubzoid run <scratchpad>/demo/finance-hub --no-ui --bridge-port 8790` started 2026-09-17 23:34, cwd = repo root, hubzoid resolved through the venv's editable install → `HubZoid/hubzoid`. `mount_portal` serves `Path(__file__).parent / "portal_dist"` with Starlette `StaticFiles(html=True)`, which reads files from disk on each request — so a fresh `npm run build` is picked up without a restart. Its log shows it served `index-CvQJ-u2B.js` earlier (a build hash that no longer exists), so the "old HTML" symptom is browser caching of `index.html`/assets (StaticFiles sends no `Cache-Control`; browsers apply heuristic freshness). Hard-reload (or DevTools "Disable cache") shows the current build. Not changed here: a backend follow-up could send `Cache-Control: no-cache` for `index.html`. The server uses `HUBZOID_PORTAL_DEV_USER=admin@demo.local` against a scratch deployment (`Finance Assistant`, `Support Assistant`, `IT Ops Assistant`, no OWUI). Not killed, no live mutations issued.

## Known gaps
- No atomic batch endpoint exists; the UI reports partial saves honestly. A backend `POST /access/apply` that applies a change set in one transaction would remove that caveat.
- `hubzoid/portal.py` module docstring still describes the old five-screen layout (backend file, left untouched on purpose).

## Checkpoint — brand, simplification, white-label, commit (2026-09-18, session end)
Committed to `auth-schedules-upgrade` (no push). All work below is on top of the
agent-first redesign already documented above.

Done this session (all verified: lint clean, `npm run build` passes, **21 journeys PASS**,
backend 53 tests pass for portal/access/reconcile/account-states, ruff clean, `git diff --check` clean):
- **Brand skin** from `HubzoidStudio/` V1: Signal Orange `#E5572A`, Inter + JetBrains Mono,
  1px borders, no shadows. Ant Design themed via `lib/theme.ts` (kept antd — did not switch
  to shadcn; the brand is fully expressible in tokens and a rewrite would discard the tested UI).
- **Light + dark** with a persisted toggle (`useThemeMode`, `data-theme` on `<html>`,
  follows system). Real `/hubzoid` wordmark (light/dark) replaces the blue "h". Neutral
  `.hz-avatar` (was blue).
- **Agents page simplified**: removed the two full-width alerts and the "N need attention"
  count — status now lives only on each card's tags. Fixed search/cards spacing and card
  button placement (primary full-width + secondary row).
- **Alerts subtler + smaller** everywhere: compact single-line banners (`.notice`) + global
  compact `.ant-alert` sizing.
- **Person drawer**: Identity / Chat account / Role descriptions moved from crowded inline
  text to a hover **?** (`HelpLabel` + `.help-icon`).
- **Sign out** added to the sidebar footer. The portal has no login of its own: it validates
  the chat app's session cookie server-side (`_verify_owui_session`); sign-out clears that
  shared same-origin session. Local-dev bypass (`HUBZOID_PORTAL_DEV`) unchanged.
- **White-label copy**: removed every user-visible "Open WebUI" mention → "the chat app"
  (customer base is 10–20 users, white-labeled; a future second UI surface must stay generic).
  Backend identifiers `owui_id` / `owui-identity` untouched (never rendered; actor shows as
  "Account sync").

Control audit (read-only, this session): every UI button/toggle/tag/filter/param is backed
by the real backend — confirmed "Public access" is real (per-hub `*` `use_hub`, org-admin only,
never anonymous). Removed one dead `case "bootstrap"` in `format.ts` (store only writes it as
an actor, never an action).

Still open (unchanged): docs step 10 (nav names in `docs/ADMINISTRATION.md`); optional backend
`POST /access/apply` for atomic multi-change saves; consider `Cache-Control: no-cache` on
`index.html` so a new build isn't browser-cached.

## Checkpoint — review-round fixes (2026-09-18, after f8756c8)
Addressed a focused review of the committed redesign. All verified: portal lint
clean, build passes, **23 browser journeys** (2 new), **76 backend tests**, ruff clean.

1. **Sign-out actually ends the session.** The chat app's cookie is HttpOnly, so a
   JS cookie delete can't clear it. `Shell.signOut` now POSTs `/api/v1/auths/signout`
   (same origin), honours any SSO logout redirect, clears the SPA's localStorage
   token, then redirects.
2. **Account state fully integrated.** `Person` now carries `status`, `suspended`,
   `account_unavailable`; the UI uses the backend's authoritative `status` (no more
   client re-derivation). `PersonDrawer` offers **Reactivate** only for an admin
   suspension; an *unavailable* chat account shows an explanatory note and no
   Reactivate. `act()` announces the backend's own message, so reactivation that
   leaves access paused says so instead of "active again".
3. **Optimistic concurrency.** `/access` returns `revision`; grant/revoke accept
   `expected_revision`; `GrantStore.revision()` added. The editor sends the loaded
   revision on the first save request and the backend 409s if another admin changed
   access since load. Backend test `test_expected_revision_guards_concurrent_edits`
   + a browser journey cover it.
4. **No editing on stale rows during refresh.** `useData` exposes `refreshing`
   (reload issued, response not yet in). AccessEditor disables Add/Edit/public while
   refreshing so a post-save refetch can't be edited mid-flight.
5. **Removed/renamed tools are removable.** The editor renders orphan grants
   (in the person's perms but not in the current catalogue) as a checked
   "No longer available" entry that can be removed but not re-added.
6. **Tests/docs match reality.** The fixture now returns the real `/people` shape
   (status + both block flags), a policy `revision`, and the `/people/block`
   message; it models an unavailable account. `docs/ADMINISTRATION.md` nav updated
   (Access tab + Review/Save; Activity → Access changes / Tool decisions; Runs &
   schedules → View runs; People sync status) — the stale "Overview → Sync now" and
   "Audit →" references are gone. Also neutralised a leftover "Open WebUI" string in
   the `/access` mutate response.

Obsolete earlier "still pending" note about `POST /access/apply`: superseded — the
optimistic-revision guard now covers the concurrency risk. Remaining truly open:
`portal/README.md` still a light dev note; consider `Cache-Control: no-cache` on
`index.html`; a full multi-request atomic apply is still not transactional (each
grant/revoke is its own request), but stale-state saves are now refused.

## Checkpoint — concurrency + investigation correctness (2026-09-18)
Two review rounds addressed. Verified: portal lint clean, build passes, **23 journeys**,
**92 backend tests** (incl. new concurrency test), ruff clean.

Round A — concurrency & lifecycle (release-blockers):
- **Atomic apply.** New `POST /access/apply` applies a subject's whole change set in
  ONE transaction, revision checked under the write lock (`GrantStore.apply_changes`
  + `_read_revision_locked`; `RevisionConflict`). The editor sends one request with
  `expected_revision`; a concurrent change 409s and nothing is written. Replaces the
  per-request loop (which guarded only the first op and could interleave).
- **Consistent snapshot.** `/access` now returns (revision, rows) from one
  `access_snapshot()` transaction, so the revision matches exactly the rows shown.
- **Account state integrated.** `AccessRow`/`Person` carry `status`+`suspended`+
  `account_unavailable`; the editor locks admin-block vs missing-account distinctly;
  Reactivate only for admin suspension; `act()` shows the backend's own message.
- **Logout is real.** POSTs the chat app's signout (HttpOnly cookie), checks `res.ok`,
  shows a failure message instead of a false success; real accessible button.
- **Refresh lock, orphan-permission removal** (from the prior round) retained.

Round B — investigation correctness (reviewer priority #1):
- **Misleading health fixed.** Removed per-workflow "Last dispatch"/"Missed" columns
  (they were agent-level values copied onto each row); shown once as an agent-level
  scheduler line. `Next run` stays per-workflow (it is per-workflow).
- **Timestamps.** Tool decisions now record UTC (`audit.py` used local `datetime.now()`
  with no tz); the UI shows the viewer's zone explicitly (`timeZoneName: "short"`).
- **Labels.** Activity person filter is "Affected person’s email" for access changes;
  a run's "See activity" is now "Agent activity" (not run-correlated).
- **A11y.** The People `?` explanations are keyboard/touch reachable (focusable +
  click trigger), not hover-only.

Still open — a scoped **investigation-UX phase** (documented for next):
1. Filters (before backend pagination): Activity time-range + actor("Changed by") +
   action/tool/outcome/channel; Runs status(Failed/Running/Successful)+date+run-id;
   People account-status/role/agent. Needs new query params on /audit, /access-changes,
   /runs, /people and the fixture + backend to filter pre-pagination.
2. Event details: click an Activity row → drawer with precise UTC time, actor, affected
   identity, agent, capability, reason, copyable ids.
3. Cross-agent "All agents → Runs" defaulting to recent failures/missed.
4. URL-persisted filters/pagination (shareable, restorable) + Person→Edit-access opening
   that person's editor directly.
5. Distinguish "no matches" vs "nothing yet" empty states + Reset filters; Last-updated /
   optional auto-refresh on active runs; link out to an existing log system for deep logs.

## Checkpoint — Priority 1 correctness + Priority 2 investigation UX (2026-09-18)
Implemented against the review. Verified: portal lint clean, tsc+build pass,
**26 browser journeys**, **137 backend tests** (+ 2 PostgreSQL apply/snapshot tests),
ruff clean on all changed files (7 remaining ruff errors are pre-existing, in files
this work never touched).

### Priority 1 — correctness (COMPLETE)
1. **Atomic apply, revision-checked under the write lock.** `GrantStore.apply_changes`
   applies a whole change set in one transaction; `_read_revision_locked` (SQLite
   no-op UPDATE / Postgres FOR UPDATE) makes the revision check + writes atomic.
   New `POST /access/apply`; the editor sends one request. `grant()/revoke()`
   refactored onto the same `_grant_in_txn/_revoke_in_txn` helpers.
2. **Consistent snapshot.** `access_snapshot()` uses a read-verify loop (re-reads the
   revision around the grant read; retries if it moved) so it's consistent on SQLite
   AND Postgres regardless of isolation. `/access` returns that (revision, rows) pair.
3. **account_unavailable suppresses effective** access in `/access` (uses the combined
   blocked state; direct grants preserved in `perms`). Enforcement already denied it.
4. **Honest save failures.** `ApiError{status, certain}`: 4xx = confirmed no-commit
   ("No changes were saved"); 5xx/network = uncertain ("Couldn't confirm whether the
   changes were saved") — reloads to show the real state, never claims success early.
   `useData.refreshing` locks the editor entry points during the reload.
5. **Timestamps.** Tool decisions now record UTC (`audit.py`); the UI shows the viewer's
   zone and renders legacy no-offset timestamps literally as "(server time)" rather than
   silently reinterpreting them.
6. **Tests:** `tests/test_access_apply.py` (9 SQLite: atomic set, revision conflict,
   cascade, orphan revoke, mid-txn rollback of grants+audit+revision, two-simultaneous-
   applies-one-wins, apply-vs-other-writer, concurrent-snapshot-consistency) + Postgres
   variants + `test_portal_api` endpoint tests (apply atomicity, unknown-perm 422, orphan
   revoke, hub-admin scope, unavailable 409, org/wildcard rejection, concurrency 409).

### Priority 2 — investigation UX (LARGELY COMPLETE; Runs deferred)
- **URL infra:** `useHashQuery` + `hrefWith`/`setQuery` in the router; filters & pagination
  live in the URL (refresh/Back/shared links restore them).
- **Activity filters (before pagination, scope-preserving):** time range, agent, affected
  person, changed-by actor, action (changes); tool, outcome (allow/deny), channel
  (decisions). Backend: `read_access_audit` + `audit.read` gained the params; `/audit`
  gained `outcome`.
- **People filters:** account status, role (admin/regular/service), agent — all scoped.
- **Activity event details (#8):** keyboard-reachable "Details" opens a drawer with precise
  timestamp+zone, actor, affected identity, agent, capability/tool, channel, reason and
  copyable identifiers.
- **Person → Edit access (#10):** opens that person's editor for the agent directly
  (`?edit=<subject>`).
- **Empty states + Reset + last-updated (#11):** "nothing matches" vs "nothing yet",
  Reset filters, and "updated <relative>" on Activity + People.
- **Tests:** journeys for People filters (+URL persistence via reload), deep-open, Activity
  outcome filter (+URL), event details; backend filter tests prove matches beyond page 1
  and that filters cannot widen scope (agent filter 403 for a hub admin).

### Remaining (documented, not done)
- Deep logs beyond the decision feed + run steps: link out to a configured logging
  destination rather than building one (no destination configured here). See the Runs
  checkpoint below for the exact integration boundary.

## Checkpoint — cross-agent Runs investigation (2026-09-18)
The Runs work deferred above is now **COMPLETE**. Verified: portal **oxlint clean**,
**tsc + vite build pass**, **29 browser journeys** (26 → 29: +3 Runs steps), and the
Runs backend suite (`tests/test_runs_cross_agent.py`, **8 tests**) green alongside the
existing `test_portal_api` (20) + workflow suites. Full `pytest` run: **1495 passed, 6
skipped, 5 failed** — the 5 failures are pre-existing `tests/e2e/` OTel + OWUI-upload +
browser-sidecar tests that need live external services; none reference runs/observe/portal
and none are touched by this branch.

### DBOS research verdict (step 1 — no new infrastructure needed)
Installed **DBOS 2.31.1**. `DBOSClient.list_workflows` natively supports every Runs
requirement, all server-side (before pagination): `status` (list), `start_time`/`end_time`
(ISO, compared to `created_at`), `workflow_ids` (run-id), `name` (list), **`application_name`
(list → multiple agents in one query)**, `limit`/`offset`, `sort_desc`. So a **thin adapter**
over `list_workflows` suffices; no bespoke query layer was built. Verified against the real
SDK (seeded `workflow_status` rows, read back through `observe.runs`/`runs_across`).

### Topology handled
DBOS system DBs are **per-bridge on SQLite** (each agent its own file) and **shareable on
Postgres**. `runs_across` groups authorized hubs by `db.dbos_url`, opens **one client per
distinct URL** querying its members via `application_name=[...]`, then **merges → sorts
(started desc, id desc) → paginates** the combined set. Correct cross-source pagination
over-fetches `offset+limit+1` newest rows per source (enough to contain the global page and
the one extra row that reports `has_more`).

### Implemented
- **Backend `hubzoid/workflows/observe.py`:** `runs()` gained `statuses`/`start`/`end`
  (single agent, still carries per-step detail on `run_id`); new `runs_across(hubs, …)`
  returns `{runs, has_more}`; `resolve_statuses()` maps UI buckets
  (succeeded/failed/running/cancelled) + raw DBOS states, dropping unknown tokens so a
  filter can never widen the query; `STATUS_BUCKETS` documents the mapping. Rows carry the
  deployment **hub key** (not the folder name) so detail links resolve.
- **Backend `hubzoid/portal.py` `/runs`:** `hub` is now optional. Named agent → that
  bridge (403 if unmanaged). No agent → **cross-agent over `allowed_hubs(admin)` only**.
  New `status` / `since` / `until` params; returns `has_more`. Filters run before paging;
  scope can never be widened.
- **Frontend:** new top-level **Runs** area (`#/runs`, `AllRunsScreen.tsx`) mirroring the
  cross-agent Activity screen, plus a **Runs** nav item (Shell) and route (Portal). Agent /
  status / time-range / run-id filters + **Auto-refresh** checkbox, **all URL-persisted**
  via `useHashQuery` (refresh, Back and shared links restore them). Columns: Agent,
  Workflow, Run, Status, Started, Duration, Result; rows link into the agent's existing run
  detail (steps). "updated <relative>", loading/error, "no matches" vs "nothing yet" +
  Reset filters.
- **Auto-refresh** (`REFRESH_MS = 10s`): a `setInterval` fires `reload()` **only when no
  refresh is in flight** (no overlapping requests); cleared on toggle-off / unmount.
- **Error recovery:** `useData` now keeps the last good rows for the *same* query when a
  refresh fails, so an auto-refresh blip shows the previous page with a "Couldn't refresh
  just now" note instead of collapsing to an error screen; a filter change (new path) still
  shows loading, never stale rows. Initial-load errors still show the full error/retry.
- **Scheduler health stays separate from run outcomes:** agent-level scheduler line +
  stale/missed/downtime alerts remain only on each agent's *Runs & schedules* tab
  (`WorkflowList`); the cross-agent Runs view shows individual run outcomes and links back
  to that tab in its subtitle.

### Tests added
- `tests/test_runs_cross_agent.py` (real DBOS `list_workflows` contract, seeded rows):
  cross-agent ordering + pagination with **matches beyond page 1** and **duplicate workflow
  names**; **status filter before pagination** (oldest failure surfaced with limit=1);
  **date window + timezone boundary** (inclusive edges, UTC); **unauthorized-agent
  exclusion**; cross-agent run-id lookup; single-agent status filter + steps;
  `resolve_statuses` buckets/passthrough; **portal `/runs` endpoint scope** (org sees all,
  hub-admin sees only finance, naming an unmanaged agent → 403, status passthrough).
- Journeys (+3): cross-agent list ordered + agent-labelled, status/agent filters before
  paging + **URL persistence across reload**, open-detail routing + **Back restores
  filters**, distinct **no-matches** empty state + Reset, and **auto-refresh** fires while
  on / keeps rows on a failed refresh / **stops on unmount**. Fixture `/runs` handler
  rewritten for cross-agent + filters + `has_more`; added a one-shot GET-failure hook.

### Deep-log integration boundary (optional, NOT invented)
Run detail surfaces what DBOS durably records: status, output, error, and per-step
output/errors (`observe.runs(..., run_id=…)` → `list_workflow_steps`). Anything beyond that
— full stdout/stderr streams or external log aggregation (Loki/CloudWatch/etc.) — is an
**external integration that is intentionally not wired here**: no destination is configured
in this deployment, and none was fabricated. When a destination exists, the intended
extension is a per-run **"View full logs ↗"** deep link built from the run id + a configured
base URL; the portal does not host or proxy logs itself.

## Checkpoint — cross-agent Runs review fixes (2026-09-18)
Fixed the four issues from the independent review. Verified: portal **oxlint clean**,
**tsc + vite build pass**, **30 browser journeys** (adds an access-editor-lock journey and a
controlled-clock auto-refresh journey; the old real-time-wait auto-refresh step was replaced,
so the count moved 29 → 30), and **13 Runs backend tests** green
(`tests/test_runs_cross_agent.py`) alongside `test_portal_api` (20).

1. **Cross-agent pagination now orders by `created_at`, consistently.** DBOS can only
   `sort_desc` by `created_at`, so each source is limited by `created_at DESC` and the merge
   now sorts by the **same** key (`_order_key` = `(created, id) desc`) — not by
   `started` (dequeued-or-created). `_run_row` exposes `created` (ordering/pagination key)
   separately from `started` (display: when execution began). The over-fetch guarantee holds
   again. Regression tests: `test_queued_run_paginates_by_created_not_dequeued` (a run created
   early but dequeued last stays on its created-order page, never floats to page 1 nor
   vanishes) and `test_equal_timestamp_pagination_is_stable` (identical timestamps paginate
   deterministically via the id tie-break, no gaps/dupes).
2. **Failed recovery refresh no longer unlocks the Access editor.** `useData`'s keep-last-good
   behaviour is now **opt-in (`{ keepStale: true }`)** and used ONLY by the read-only Runs
   view. Access (and every mutating editor) keeps the default: a failed refresh drops to the
   error+**Try again** state (no editable controls) and unlocks only after a successful fresh
   response. Regression journey: *"A failed post-save refresh keeps the access editor locked
   until a successful retry."*
3. **Invalid status filters are rejected, not widened.** `resolve_statuses` now distinguishes
   *absent* (None/""/whitespace → `None`, no filter) from *supplied-but-unknown* (→
   `ValueError`); `/runs` maps that to **422** before the generic 503 catch. A stray filter can
   never collapse to "no filter" and return every status. Tests:
   `test_resolve_statuses_buckets_passthrough_and_rejects_invalid` and
   `test_portal_runs_endpoint_rejects_invalid_status` (422 + valid still 200); the old test
   that expected `bogus → None` was corrected.
4. **Relative time windows are live vs fixed, explicitly.** The cutoff is anchored (no
   render-loop) but a live auto-refresh with a relative range now **advances it** each tick
   (`setNowMs(Date.now())` → new `since`); with no range it re-fetches the same query; when
   auto-refresh is off the window stays fixed (predictable pagination, shareable). Verified
   with Playwright's **controlled clock**: the *"Runs auto-refresh (controlled clock)"* journey
   asserts one request per interval (no overlap), the live `since` advancing after the clock
   moves, read-only error recovery keeping rows, and the interval stopping on unmount.

### Full-suite status (this environment)
`pytest` (full): all suites pass **except the same 5 pre-existing `tests/e2e/` failures**, which
need live external services (an OTel collector; a running chat-app upload backend):
- `tests/e2e/test_otel_e2e.py::test_otel_emits_attributed_metrics_through_runtime`
- `tests/e2e/test_otel_e2e.py::test_normalize_converts_real_claude_span_attrs`
- `tests/e2e/test_owui_upload_single_store_e2e.py::test_owui_document_lands_in_single_store`
- `tests/e2e/test_owui_upload_single_store_e2e.py::test_owui_image_gets_vision_marker`
- `tests/e2e/test_owui_upload_single_store_e2e.py::test_unresolved_owui_file_is_loud_not_silent`

**Demonstrated pre-existing:** stashing all work back to the comparison commit `f8756c8` and
running these five in the same environment reproduces all five failures identically (26s), so
they are environmental, not regressions from this branch.

### Remaining limitations (actual)
- The 5 e2e tests above require live external services and cannot pass in this offline
  environment (unchanged by this work).
- On a **live sliding window** (auto-refresh + relative range), each tick changes the query
  (new cutoff), so the read-only keep-last-good recovery does not apply to that specific tick —
  a failed slide shows loading/error for the new window rather than retaining the prior
  window's rows. Same-query refreshes (the common case, and any fixed window) do retain rows.
- Deep external logs remain a documented integration boundary, not a shipped feature.

## Checkpoint — deep review fixes: equal-timestamp pagination + persisted windows (2026-09-18)
Fixed the two deeper review issues. Verified: **oxlint clean**, **tsc+build pass**, **30 browser
journeys**, **33 targeted backend tests** (`test_runs_cross_agent` 12 + `test_portal_api` 20 +
Postgres equal-timestamp 1), ruff clean.

1. **Equal-timestamp pagination no longer skips/duplicates.** DBOS orders each source by
   `created_at` ALONE, so truncating the over-fetch could split a tied group and re-sorting the
   window by id produced the reported `c,b,c,b,…` skips/dupes. `runs_across` now, once the page
   boundary is known, **refetches the COMPLETE group at exactly the boundary timestamp** before
   applying the id tie-break (`_query_source` with `start=end=boundary`, `limit=None`). This is
   bounded — all rows newer than the boundary already fit the initial over-fetch (proven: no
   single source can hold more than the window's worth of strictly-newer rows without pushing the
   boundary up), and only the one boundary timestamp's group is refetched, never full history.
   `has_more` uses a single bounded 1-row probe per capped source when the page ends on the
   boundary. Tests: the exact reproduction (one source, 10 tied rows j..a, page 2 → concatenated
   == full), a cross-source split variant, and **`tests/test_postgres_acceptance.py::
   test_equal_timestamp_pagination_postgres`** (shared-Postgres DBOS system DB).
2. **Fixed/shareable time windows are actually persisted.** Runs and Activity now store
   **absolute `since`/`until` in the URL** for fixed investigations (frozen at selection), so
   reload and shared links reopen the same window regardless of the clock, and the fixed upper
   bound stops new runs from shifting later pages. Live relative windows are explicit and
   separate (Runs auto-refresh: `range` + `auto=1`, cutoff slides on each tick; only Runs has a
   live mode — Activity is always fixed). `/runs` and both Activity endpoints receive `until`.
   Controlled-clock journey asserts a fixed window retains its absolute since/until across a
   clock advance + reload, while the live window's cutoff advances.

## Checkpoint — automatic upgrade-on-startup: INVESTIGATION (2026-09-18)
New task: replace the manual `access migrate` cutover with an automatic upgrade the customer
gets by installing the new version and restarting with the same config. This checkpoint is the
mandatory first step — grounding the work in the REAL deployments and the existing machinery,
before writing migration code. No implementation yet.

### Real deployments & legacy access sources (on-disk evidence)
Inspected read-only under `/Users/shreyarao/Desktop/WaveAssist/Hubzoid/`:
- **HubzoidTestHub/test-hub** — legacy single hub: `identity/access.csv` (columns
  `phone,email,groups,center` — the Isha identity model), per-hub `.hubzoid/hub.db` + `dbos.db`
  + existing `.hubzoid/backups/`, OWUI `.openwebui-data/webui.db` (newest schema: has
  `access_grant`, `scim`, `variables`, `config` 341 rows; 1 admin + 1 pending user). Safe
  rehearsal target (synthetic test data).
- **SamarthDiamond/SamarthDiamondHub** — customer: OWUI `.openwebui-data/webui.db` on an
  **OLDER Open WebUI schema** — has `migratehistory`, **NO `access_grant` table**, no `scim`
  column, `config` 1 row; 1 user (role=admin/owner), 0 groups, 0 models. `.hubzoid/` has only
  `chats/` (no hub.db/grants → new access system never ran here). NO identity CSV → its only
  legacy access source is OWUI accounts.
- **SamarthJewellery/SamarthJewelleryHub** — customer: OWUI `webui.db` on a **NEWER schema**
  (has `access_grant`, `scim`); 1 admin/owner, 0 groups, 0 models; empty `.hubzoid/`.
- **Isha (irs/gpms)** — referenced by memory + a STALE test gateway config
  (`.hubzoid-gateway/deployment.json`, whose paths point at `pytest-of-.../pytest-145/...`, i.e.
  a leftover test artifact, NOT customer data). **Isha's real prod configuration/data is NOT
  available in this workspace** — per the task, this is reported, not assumed. Isha prod may run
  OWUI on PostgreSQL and uses the CSV identity model; that must be rehearsed against a protected
  copy of their box before claiming Isha compatibility.

**Honest data-availability statement:** rehearsal is possible against HubzoidTestHub (legacy CSV
+ OWUI) and against copies of the two Samarth OWUI DBs (SQLite). Isha's live box is unavailable
here. I will NOT claim customer compatibility from synthetic fixtures alone; Samarth Diamond in
particular exposes a real schema-compat gap (below).

### Real compatibility gap already visible
SamarthDiamond runs an OLDER OWUI with **no `access_grant` table** (0.9.6 replaced the
access_control column with the async `access_grant` table — see memory `owui-096-provisioning-api`,
`owui-nonadmin-empty-model-list`). Visibility projection / reconcile that assumes `access_grant`
will not work there. Per the safety rule ("if an automatic migration cannot be proven safe,
retain that hub's legacy behavior and surface an actionable failure"), the old-schema case must
be detected pre-cutover and either handled explicitly or left legacy with a clear message — never
cut over blindly. Both customers also have 0 OWUI models seeded, which the current
`access migrate` treats as `MigrationBlocked` for registered gateways ("requires OWUI model
evidence") — the auto-startup path must account for the no-model case.

### Existing machinery to REUSE (no new migration platform)
- `hubzoid/access/migrate.py` (441 lines): `plan_from_csv`, `plan_from_owui`,
  `plan_standalone_public`, `MigrationBlocked` — the plan builders the CLI already uses.
- `hubzoid/cli.py` `access migrate` (~1470-1560): current MANUAL invocation; the reference for
  evidence checks (OWUI model evidence, model-ID match) and standalone-vs-registered logic.
- `hubzoid/access/store.py`: the idempotency/authority backbone already exists —
  `hz_meta` markers `bootstrapped` and per-hub `casbin_authoritative:<hub>` (+ global);
  `is_authoritative(hub)`, `any_authoritative()`, `set_authoritative(flag, hub=)`,
  `bootstrap(admins, authoritative=, hub=)` ("given admins once, so no deployment can lock
  itself out"). This is exactly the "already-migrated → never re-import; revoked admins stay
  revoked" marker the task requires.
- `hubzoid/access/owui.py`, `reconcile.py`: OWUI identity/account/admin discovery + visibility
  projection (the OWUI DB URL comes from `deployment.owui_db`/`owui_url`).
- `hubzoid/deployment.py`: manifest (`.hubzoid-gateway/deployment.json`: `hubs[]` with
  key/name/path/model_id/dbos_url, plus `operational_url`, `owui_url`, `owui_db`).
- `hubzoid/server.py`: boot sequence — the natural home for the startup hook.

### Design direction (to implement next, reusing the above)
On first upgraded startup, per hub, inside one coordinated step:
1. If `is_authoritative(hub)` already → SKIP (idempotent; never re-import over dashboard edits).
2. Validate OWUI schema/credentials + discover the real OWUI DB (SQLite/Postgres). If the hub
   can't be proven safe (e.g. Diamond's missing `access_grant`) → retain legacy, surface an
   actionable failure, do NOT mark complete.
3. Automatic consistent backup (reuse `.hubzoid/backups/`) of operational + OWUI state; record a
   restorable, identified snapshot; detect source changes during migration.
4. Block portal/API permission edits for that hub until cutover completes (guard on
   `is_authoritative`), so nothing the migration overwrites is reported as effective.
5. Build the plan via `migrate.plan_from_csv`/`plan_from_owui`; `bootstrap(verified OWUI admins,
   authoritative=True, hub=hub)` once, with an audit record; coordinate visibility sync; verify
   allowed AND denied cases; only then mark the hub authoritative/complete.
6. Preserve an operator rollback command (exceptional use), not part of the normal path.
7. Snapshot today's group/SSO membership without breaking tomorrow's onboarding (future users).

### Status / release-readiness
Investigation complete; implementation NOT started. NOT release-ready. Exceptions already
requiring attention: SamarthDiamond old-OWUI schema (no `access_grant`); 0 models on both Samarth
hubs; Isha prod data unavailable for rehearsal here. Do not commit/push.

## Checkpoint — automatic upgrade-on-startup: IMPLEMENTED (2026-09-18)
Implemented the automatic startup migration by REUSING the existing machinery (no new
platform). New file `hubzoid/access/startup.py` (`auto_upgrade`/`upgrade_hub`); wired into
`hubzoid/server.py` `_lifespan` (runs in a thread BEFORE the app yields → before any request
is served). Operator docs added to `docs/ADMINISTRATION.md` ("Automatic upgrade on startup").

### Behavior (per hub, idempotent, safe-or-legacy)
1. `is_authoritative(hub)` → skip (repeat/interrupted starts never re-import over dashboard
   edits; revoked admins stay revoked).
2. Discover real OWUI source as a SQLAlchemy URL: `HUBZOID_OWUI_DB_URL` (Postgres DSN) →
   manifest `owui_db` → local `webui.db`. Bare hub (no OWUI + no manifest) → `skipped` (never
   auto-cut-over a dev `run`).
3. Build plan via `migrate.plan_from_csv` + `plan_from_owui` (or `plan_standalone_public`).
   Gates mirror the CLI: model evidence required, non-empty plan, `verify_effective` (checks
   allowed AND denied AND `__future_signed_in__`), and a verified OWUI admin must exist.
   Any block → hub left `legacy` with an actionable reason (never partial/broadened).
4. `bootstrap(admins, authoritative=False)` establishes dashboard admins once (audited;
   `bootstrapped` marker makes it a no-op later). Then a 0600 backup (same format
   `access rollback` restores) → `migrate.apply(authoritative=True)` (atomic cutover) →
   zero-diff gate; on non-zero diff it auto-rolls-back via `store.restore`.
5. Visibility projected via `reconcile.sync_owui` (best-effort; failure recorded, not fatal —
   chat entry is enforced from the authority, not the mirror).
6. Group→grant flattening surfaced as `onboarding_note` (future SSO/SCIM members need an
   explicit grant — not silently dropped).

Edit-lock: because it runs before `yield`, no portal/API edit can race the cutover; the
per-request gate (`_enforce_use_hub`) stays legacy pass-through until authoritative, so
mid-migration state is never reported effective.

### Tests (all green)
`tests/test_startup_migration.py` (14) + `tests/test_postgres_acceptance.py::
test_startup_migration_on_postgres_operational_store` (1): old-state→upgrade (allowed/denied/
admin/tool-perm), public→future-signed-in allowed, restart idempotency + revoked-admin-not-
restored, post-upgrade edit not re-imported, interrupted cutover→legacy→recovers, visibility
failure non-fatal, zero-diff auto-rollback, operator rollback, no-model→legacy, bare-dev-run
skipped, multi-hub isolation (shared SQLite operational DB), legacy OWUI schema
(access_control JSON), and **rehearsal against protected COPIES of the real SamarthDiamond &
SamarthJewellery OWUI DBs** (both currently have no OWUI model → safely left legacy with a
reason; originals untouched). PostgreSQL operational-store variant covers the shared-DB config.

### Customer-specific evidence & release-readiness
- **SamarthDiamond, SamarthJewellery** (real OWUI DBs on disk, rehearsed via copies): both have
  **0 OWUI models registered today**, so auto-upgrade safely NO-OPS to `legacy` with an
  actionable reason — it does not cut over, does not broaden access. This is the correct, safe
  outcome; to actually migrate them their hub must be registered as an OWUI model first.
  SamarthDiamond additionally runs an older OWUI schema (no `access_grant`); the legacy
  `access_control` path is supported and tested.
- **Isha (irs/gpms)**: real prod config/data NOT available in this workspace (only a stale test
  artifact). Compatibility must be rehearsed against a protected copy of their box (likely
  OWUI-on-Postgres + CSV identity) before release. NOT claimed here.
- **Release-readiness:** the mechanism is implemented, reused, and tested (SQLite + Postgres,
  the full scenario matrix). It is **safe to ship for the Samarth topology** (worst case: safe
  legacy no-op). **NOT release-verified for Isha** pending a rehearsal against their real box.
- **Exceptions still requiring attention:** register OWUI models for the Samarth hubs before
  expecting an actual cutover; obtain an Isha prod copy to rehearse; the 5 pre-existing
  `tests/e2e/` failures (external services) remain unrelated.

Do not commit or push.

## Checkpoint — scope correction: manual upgrade, auto-startup removed (2026-09-19)
Final scope: ONE controlled deployment, a planned MANUAL upgrade is acceptable. The
automatic-startup migration is SUPERSEDED and removed.

### Removed (complexity that only existed for auto-startup)
- Deleted `hubzoid/access/startup.py` and `tests/test_startup_migration.py`.
- Removed the `auto_upgrade` call from `hubzoid/server.py` `_lifespan` (server boot no
  longer migrates; the only access work on boot is the OWUI visibility loop, unchanged).
- Replaced the PG "startup migration" test with `test_explicit_migration_on_postgres_
  operational_store` (the manual cutover primitive on Postgres).
- Rewrote the ADMINISTRATION.md "Automatic upgrade on startup" section into a concise
  **planned maintenance procedure** (stop writers → back up → bootstrap admins →
  migrate each hub → verify → restart → smoke-test), reusing existing CLI commands.

### Added / changed (correctness for the manual path)
- **Re-import guard:** `hubzoid access migrate --apply` now refuses an already-migrated
  hub (`is_authoritative`) unless `--remigrate` is passed — prevents overwriting edits
  made after migration.
- **Legacy hubs are read-only in the dashboard, enforced in the API not just the UI:**
  `/access` returns `editable=false` for a non-authoritative hub; `/access/grant`,
  `/access/revoke` and `/access/apply` return 409 for hub-scoped edits on a legacy hub
  (org-admin `*` management still allowed). AccessEditor locks its controls and shows a
  "managed in the chat app" banner. So a legacy agent's edits can neither appear
  effective nor be silently overwritten by migration.
- **Audit time filtering fixed to compare INSTANTS** (`audit.py`): `since`/`until` and
  each row `ts` are parsed to tz-aware datetimes (naive read as UTC), so a window with a
  `Z`/`+00:00`/other offset selects the same events (was lexical string compare).
- **Debounce timer cleanup on unmount** in Activity, People and Runs screens, so a
  pending keystroke timer cannot rewrite the next screen's URL after navigation.

### Review findings status (item 4)
- Audit instant comparison — FIXED now (`tests/test_audit_time_filter.py`).
- Activity/People/Runs debounce focus + timer cleanup + no cross-page URL write — FIXED
  now (unmount cleanup added; focus preserved via keyed uncontrolled inputs, unchanged).
- Unavailable-account offboarding — verified closed: grant to an unavailable/suspended
  account is refused; revoke of its direct grants is allowed; reactivation of an
  unavailable (chat-gone) account is not offered (journey + `test_portal_account_states`).
- Cross-agent equal-timestamp pagination — closed earlier (boundary-group completion;
  SQLite + Postgres tests).
- Fixed vs relative time windows — closed earlier (absolute since/until persisted;
  controlled-clock journey).
- Atomic revision-guarded saves / consistent snapshots / stale-data protection — intact
  (`test_access_apply`, `test_portal_api`, PG snapshot-consistency test).

### Demonstrated dashboard path (requirement: a workable path, not just legacy fallback)
`tests/test_migration_to_dashboard.py::test_manual_migration_makes_the_agent_dashboard_
managed` runs the real cutover (plan_from_owui + apply) then drives the portal API:
legacy→read-only, after migration editable=true with allowed/denied preserved and the
dashboard admin established, a dashboard grant takes effect, and operator rollback
returns it to legacy read-only (survives a store restart). The OWUI→dashboard link
(admins only) is covered by `test_edge.py`; sign-in uses the verified OWUI session and
sign-out clears it (Shell + `_verify_owui_session`).

### Real deployment identification & prerequisites
- On-disk real OWUI DBs: SamarthDiamond (older OWUI schema, no `access_grant`) and
  SamarthJewellery (newer). Rehearsed against protected COPIES
  (`test_rehearse_migration_against_real_customer_owui_copy`): both currently have **no
  registered OWUI model** for the hub, so migration correctly FAILS CLEARLY before
  changing anything — the prerequisite is to register each hub's OWUI model first.
- The "one important deployment" (Isha irs/gpms per memory) is NOT present in this
  workspace. To migrate it, the exact inputs needed: its OWUI source DB (SQLite path or
  Postgres URL), each hub's OWUI model ID, the gateway admin email/password for
  visibility sync, and the existing OWUI admin email(s). Not guessed.

### Continuation instructions
1. On a protected copy of the real box: run the ADMINISTRATION.md maintenance procedure
   per hub; confirm `access diff` is 0/0 and the smoke-test passes.
2. Register each hub's OWUI model if missing (migration blocks without it).
3. Confirm whether onboarding relies on OWUI group membership; if so, plan explicit
   dashboard grants for post-migration joiners.
4. Do not commit/push or touch the live deployment.

## Checkpoint — five follow-up gaps closed (2026-09-21)
Bounded fixes to the manual-upgrade work; no new infrastructure.

1. **Maintenance sequence corrected (docs).** ADMINISTRATION.md now says to stop bridges +
   the visibility projector but keep OWUI running PRIVATELY (loopback, with
   OWUI_INTERNAL_URL/WEBUI_URL + gateway admin creds) during the window, because
   `access sync` and rollback's visibility restore need OWUI's API — which stopping the
   gateway would otherwise kill.
2. **Verify against the pre-cutover baseline, not a re-read after sync.** `access sync`
   rewrites OWUI model visibility (public → explicit per-user grants), so a `diff
   --from-owui` run AFTER sync compares against a changed source and falsely mismatches.
   Docs now run the confirming `diff` immediately after `--apply` and BEFORE `sync`, keep
   the 0600 backup as the baseline, and verify projection separately (idempotent second
   `sync`). Regression: `tests/test_migration_to_dashboard.py::
   test_verification_must_use_pre_cutover_baseline_not_reread_after_sync` reproduces
   zero-diff-before / nonzero-after with the store unchanged.
3. **Activity/People/Runs filter focus + timers.** Text inputs are now keyed by a
   per-screen `resetToken` (bumped only on Reset), not by their changing value, so a
   debounced commit no longer remounts the input and steals focus. Activity gives each
   text input its OWN debounce timer (tool/channel previously shared one and cancelled
   each other). Journey `Activity text filters keep focus while typing and don't cancel
   each other` asserts focus is retained and both tool+channel commit.
4. **Unavailable-account offboarding.** `lockFor`/`canRemoveAll` (plan.ts) now allow
   REMOVING a blocked/unavailable account's existing grants (offboarding) while still
   blocking NEW grants; the orphan-perm checkbox is no longer disabled for blocked
   accounts; People shows "Block access" for an unavailable-but-not-suspended account
   (explicit offboard, which the backend already implements by deleting all grants).
   Coverage: journey `An unavailable chat account is not reactivable, but CAN be
   explicitly offboarded` + `tests/test_portal_api.py::test_unavailable_account_can_be_
   offboarded` (grant refused, revoke allowed, block drops all grants).
5. **OWUI navigation across login/logout.** `portal_navigation.SCRIPT` rewritten to
   re-evaluate the session (add the link when `/portal/api/me` authorizes, remove it
   otherwise) on load, `popstate`, tab focus, and a 15s interval — so it appears after
   SPA login and disappears after SPA logout, not just once per page load. Journey `OWUI
   navigation link appears for an admin session and disappears after logout` drives the
   real injected script with a toggled `/portal/api/me`.

**Production-copy rehearsal remains explicitly PENDING** until performed on a protected
copy of the real box (needs its OWUI DB URL/type, per-hub model IDs, gateway admin
creds, admin emails). The migration→dashboard path is demonstrated in code
(`test_migration_to_dashboard.py`), not yet against live data.

Browser: 33 journeys. Backend: see the run below. No commit/push; live deployment untouched.
