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
