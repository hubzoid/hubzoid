# Native chat design polish

## Problem and scope

The founder's review identified oversized personal forms, fragmented settings,
inconsistent embedded management controls, double focus outlines and an
unfinished sign-in experience. The requested outcome is a restrained, modern
team workspace using the existing components and dependencies.

## Changes

- Group personal settings under Preferences with Profile, Appearance,
  Notifications and Data tabs. Keep My connections and schedules accessible.
- Group deployment Chat defaults and Brand under Workspace settings.
- Label the cross-agent audit screen Activity. Keep account management separate
  from an agent's permissions, with the existing routes and checks intact.
- Use compact aligned forms, flat management sections, quiet actions and a
  single visible focus indicator for compound inputs.
- Refine email/password and provider sign-in without changing authentication.

## Boundaries

No new dependencies, APIs, data model, runtime or standalone Console changes.
Keep technical metadata behind disclosure and preserve review/apply controls.
Keep existing URLs, test IDs, and accessibility labels where practical.

## Validation

Use a five-hub test deployment. Review owner and member surfaces at 1470×860,
1024×768 and 390×844 in light, dark and black appearances. Run frontend type,
lint and build checks, existing browser journeys and focused regressions for
navigation and focus. Record live OAuth/provider gaps separately from fixtures.
Rebuilt assets are a separate commit. Do not push or open a PR.

The founder authorized review followed by fixes in this session. This document
records that bounded scope before implementation.

## Annotated follow-up

The founder's browser review authorized a second visual pass: adapt the agent
dashboard to the narrow Settings pane, fix People identities, explain schedules
versus workflows and branding publication, reduce repeated health/usage content,
add space below the sidebar identity, and refine sign-in using Open WebUI as a
visual reference. Seed fictional connectors in the isolated test deployment and
provide a clearly labeled Google sign-in design preview without credentials.
Shared management components may accept an optional embedded presentation; their
standalone defaults stay unchanged. Theme reduction and a guided inbound MCP
connection panel are recommendations, not backend changes in this patch.

## Merged-branch review

The founder requested another review of daily team use on the merged branch.
Observed gaps: crowded Access and workflow tables, nested interactive agent tabs,
low-contrast breadcrumbs, keyboard-inaccessible scrolling in eval tables, and
ambiguous hub context in Agent instructions. Recompose the embedded views with
existing controls, preserve standalone defaults and all review/actions, and
verify populated sources, member permissions and mobile layouts. Test fixtures
remain fictional and outside the repository.

## Approved Studio implementation · 2026-10-20

The founder approved the Studio reference and requested applying it to the actual app with a PR to main. Implement orange principal actions, neutral routine actions, explicit readable hover/active/disabled states, underline tabs, consistent geometry and keyboard focus, and neutral dropdown selection. Reuse the existing Radix Select and Ant Design controls; keep their behavior, permission checks, API contracts and custom theme overrides. No new dependencies or component-library migration are needed.

Files: shared control styles/components, native select consumers, embedded management component tokens, and the existing server palette contract plus matching first-paint CSS. Validate light/dark/OLED and branded themes, populated management sections, menus/dialogs, owner/member views, keyboard and phone targets. Use Vite source checks while developing. The founder subsequently requested no build or heavy unit runs because of RAM pressure; production bundles remain unchanged and unfinished checks are recorded at handoff. User authorization covers implementation and opening the PR; a human merges it.
