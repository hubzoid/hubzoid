# Hubzoid administration portal and chat app

React/TypeScript UI served by the existing bridge. No Node service is needed in
production; the built assets ship in `hubzoid/portal_dist`. One bundle holds two
apps: `src/main.tsx` renders the Admin Console under `/portal/` (hash routes,
Ant Design) and the chat app everywhere else (`/`, `/c/:id`, `/s/:id`, `/auth`,
`/account`; history routes, assistant-ui and Tailwind, in `src/app/`). Each is
its own lazy chunk, so neither loads the other's libraries.

The portal manages per-agent access and displays accounts, permissions, workflow
execution and audit history. Open WebUI owns accounts/authentication; Casbin owns
agent access; DBOS owns execution history. See [the operator guide](../docs/ADMINISTRATION.md).

```bash
npm ci
npm run dev       # /portal/api and the web app API proxy to the bridge on localhost:8000
npm run lint
npm run build     # typecheck + production assets
npm test          # Console: Playwright browser regressions against built assets
npm run test:app  # chat app: Playwright journeys against tests/app-fixture-server.cjs
npm run fixture:app -- 3410   # the chat app on synthetic data at http://127.0.0.1:3410/
```

The chat app journeys start the in-memory fixture themselves (sign-in and local
mode), run an axe accessibility scan of the main screens and write screenshots
to `APP_SHOTS` (default: a temporary folder). `BASE_URL=http://host:port` with
`HZ_EMAIL` and `HZ_PASSWORD` runs the journeys that don't need the fixture's
scripted replies against a real Hubzoid server instead. All chat app strings
live in `src/app/i18n/en.ts`.

`npm test` needs a Playwright Chromium installation. It intercepts requests with
synthetic accounts and verifies hub switching, correct mutation targets, error
recovery, workflow details, audit history and role controls. Python acceptance
tests in `tests/test_admin_journey.py` exercise the real APIs, shared deployment
configuration, migration/rollback, access isolation and real DBOS execution.

For development, bootstrap a local admin, then set BOTH `HUBZOID_PORTAL_DEV=1`
and `HUBZOID_PORTAL_DEV_USER=<admin>` on the local bridge. Never use these on a
public deployment. Production uses the OWUI session cookie verified server-side.

Permissions are backend-enforced. Disabled frontend controls are only UX.
Hub-dependent screens are keyed by hub; requests are aborted on navigation and
mutations use the loaded response's hub, never stale rows with a new selection.
