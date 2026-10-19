# Remove Open WebUI and the standalone Console

## Problem
Hubzoid 2.0 has its own chat and one Settings page. Open WebUI is still in the
code as a second way to run a deployment. Every feature has to keep working in
two interfaces, two account systems and two sign-in paths. The standalone
Console at `/portal/` duplicates the management screens that Settings already
shows.

## Why now
The 2.0 chat is on `main`. Every later 2.0 feature would otherwise carry Open
WebUI branches, and the removal only gets larger. Deployments that still need
Open WebUI stay on the 1.x line.

## What should happen
- **One interface.** `hubzoid run` and `hubzoid gateway` start the Hubzoid web
  app, or no UI with `--no-ui`. There is no Open WebUI mode, no
  `hubzoid[openwebui]` extra, no Open WebUI image and no account sync.
- **Stale settings are named, never ignored.**
  - Starting with `HUBZOID_UI=openwebui` (or `open-webui`, `owui`, `legacy`), or
    with a deployment recorded in Open WebUI mode, stops with a message that
    points to the upgrade guide.
  - `WEBUI_AUTH` and `WEBUI_URL` also stop the start until they are renamed to
    `HUBZOID_AUTH` and `HUBZOID_PUBLIC_URL`, even when blank or false, so sign-in
    can never turn off because an old name was dropped.
  - Other retired Open WebUI settings appear as warnings in `hubzoid doctor` and
    at start.
- **Kept:**
  - the OpenAI-compatible `/v1` API;
  - native `--no-ui`;
  - the edge's refusal of identity headers sent by clients;
  - the personal MCP connectors, under a native name;
  - the code that reads an Open WebUI database for `hubzoid migrate`, which moves
    to `hubzoid/migration/openwebui/`.
- **Standalone Console removed.** `/portal/` sends people to the matching
  Settings section. The management screens and their `/portal/api` routes stay,
  because Settings uses them.
- **Docs** for setup, deployment, accounts, branding, MCP and upgrades describe
  only the Hubzoid web app. Release history and past proposals stay as they are.

## Out of scope
- The full Open WebUI importer, rehearsal and cutover tooling. The read-only
  readiness report and the existing migration commands stay.
- Renaming the account identity column, which needs its own forward migration
  and backfill.

## Risks
- A deployment upgraded with Open WebUI settings stops at start instead of
  running. This is intended: the message says what to change.
- People who used Open WebUI API keys must create native keys.
