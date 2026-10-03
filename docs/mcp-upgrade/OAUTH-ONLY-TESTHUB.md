# OAuth-only update and TestHub verification

Date: 2026-09-29. Supersedes the legacy/dual migration design in earlier proposals.

## Changes

- Hosted MCP accepts only Hubzoid-issued OAuth credentials.
- Removed the static OWUI API-key verifier and legacy/dual mode selection.
- MCP no longer enables OWUI API-key minting.
- Open WebUI login sessions and internal bridge authentication remain operational.
- Existing OWUI API keys are not deleted, but they cannot authenticate MCP.

## Verification

169 targeted tests passed across OAuth, MCP transport, gateway, permissions,
OWUI launcher configuration and SQLite/PostgreSQL database backends.

The real TestHub is running from the SDK worktree `hubzoid-mcp-oauth` at
http://127.0.0.1:3080, with MCP at http://127.0.0.1:3080/mcp.
Anonymous MCP requests return 401; both OAuth discovery endpoints return the
correct local resource/issuer, and public client registration succeeds.
There was no existing static API key in this hub to test; valid static keys are
rejected by the automated fixture tests.

**Pending:** user login with the existing OWUI account, consent, live tools/list,
read-only whoami call, refresh and revocation. A local callback test client is
waiting on port 8767. No external GitHub/Odoo actions were invoked.

## TestHub configuration

Actual hub: `HubzoidTestHub/test-hub`.
Its `.env` now includes:

```dotenv
MCP_SERVER=true
MCP_PUBLIC_URL=http://127.0.0.1:3080/mcp
WEBUI_AUTH=true
HUBZOID_SCHEDULES=false
```

The original environment and databases were backed up to
`HubzoidTestHub/.mcp-oauth-backup-20260929-164107` before startup.
Scheduled workflows are disabled for this test run.

## Connect local Claude Code

```sh
claude mcp add --transport http --scope user hubzoid-test http://127.0.0.1:3080/mcp
claude
```

Run `/mcp`, select hubzoid-test, authenticate through Open WebUI, and allow
Hubzoid consent. Remove an older configuration containing a static Authorization
header before reconnecting. This loopback URL works for local Claude Code;
Claude web needs a reachable HTTPS deployment. See `../mcp-oauth-claude.md`.
