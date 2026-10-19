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
