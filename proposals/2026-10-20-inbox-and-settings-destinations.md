# Inbox and four Settings destinations

## Problem

A walkthrough of the 2.0 web app on 20 October, on a launch review deployment
(nine hubs, an administrator and a teammate), found it hard to find things and
hard to read:

- Notifications were a small bell popover at the foot of the sidebar, and
  approvals lived in a separate "Waiting for you" row that only appeared when
  something was pending. There was no one place to see what needs you.
- Settings had sixteen sections in one list, with no clear difference between
  them, and a separate page for each.
- An agent had nine tabs (Access, Connectors, Setup, Secrets, Channels, Intake,
  Runs & schedules, Evals, Activity), so the page felt heavy.
- Schedules mixed "reports you can run", recent requests and a table of
  schedules on one page.
- Usage limits were a YAML text box with the month's usage as plain numbers.
- Some tables opened a row when you clicked anywhere on it; others only through
  a Details button.

## What should happen

- **Inbox at the top of the chat sidebar**, in place of the bell and Waiting
  for you. It is a page, like a code host's notifications: Needs you
  (approvals and questions you can answer, answered right there), Updates
  (replies that finished elsewhere, and requests waiting on someone else) and
  Done (what was decided). Each row opens as a whole.
- **Settings with four big destinations**: Agents, Schedules, Usage and Team,
  with Back to chat as before and a quieter personal group underneath
  (Preferences, Connections, Memory, My work items). A destination holds its
  sections as tabs, not as separate pages. Team holds People, Activity,
  Permissions, Sign-in, Workspace and Health.
- **Agents as a list**: one row per agent with what its folder holds (the
  counts stand out in the accent colour), who can use it, chats and spend for
  30 days, when it was last used and whether it needs attention. The server
  counts the folder without loading it. Descriptions stay in the chat agent
  picker, where people choose an agent, not in this list.
- **A lighter agent page**: an Overview first, then fewer tabs, with related
  parts as sub-tabs (Instructions holds the files and the changes to review;
  Runs & schedules holds workflows, work items and sandboxes; Setup holds
  settings, secrets, channels and intake).
- **Usage as bars**: each agent's month against its limits, turning amber at
  80% and red at the limit, with the common limits as fields and the whole
  `budgets.yaml` one click away.
- **Schedules with tabs**: Yours, Everyone's, Agent workflows (every agent's
  own timetables in one list) and Reports.
- **One row rule**: a row opens as a whole; small actions sit at its end. No
  Details buttons.

## Scope and non-goals

- No change to permissions, APIs (other than the folder counts added to the
  hubs list), the data model or the runtime.
- Old links keep working: old Settings hashes open inside their destination,
  `/requests` opens the Inbox, `#/budgets` opens Usage.
- Not in this change: a loading placeholder for the 30-day totals, the light
  theme review of every new page, and editing another hub's instructions from
  its agent page (the Instructions tab still edits the deployment's own hub,
  as Agent instructions did).

## Open questions

- Whether the Inbox should also list work-item send approvals, hub-change
  reviews and held intake, which today wait on their own pages. This change
  keeps the sources it had.
- Whether Usage should show per-person use for teams with many repeated
  requests.

## Definition of done

The pages are reviewed on the launch review deployment and accepted. The
Settings, design-control, reports, console and real-server journeys pass with
the new screens.
