# Hubzoid administration portal

React/TypeScript UI served by the existing bridge at `/portal/`. No Node service
is needed in production; the built assets ship in `hubzoid/portal_dist`.

The portal manages per-agent access and displays accounts, permissions, workflow
execution and audit history. Open WebUI owns accounts/authentication; Casbin owns
agent access; DBOS owns execution history. See [the operator guide](../docs/ADMINISTRATION.md).

```bash
npm ci
npm run dev       # /portal/api proxies to the bridge on localhost:8000
npm run lint
npm run build     # typecheck + production assets
npm test          # Playwright browser regressions against built assets
```

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
