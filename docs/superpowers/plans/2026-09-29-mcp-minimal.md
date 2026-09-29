# Minimal MCP OAuth upgrade

User authorization: plan then implement locally, preserving existing token connections,
using existing Open WebUI login followed by Hubzoid consent, and document Claude setup.
This implementation plan supersedes the larger September 28 proposal for this first release.

## Design and boundaries

Use the already installed FastMCP OAuthProvider and Python MCP SDK for discovery,
registration, redirect validation, PKCE and token endpoints. Hubzoid owns durable
credential/grant storage, existing-account verification, consent, revocation and
resource binding. No additional identity provider, sidecar, React build or model runtime.
This is an explicit change from the earlier external authorization-server proposal:
fewer deployment dependencies, but Hubzoid must maintain and test its provider logic.

Keep MCP_AUTH_MODE=legacy by default. Opt into dual or oauth with MCP_PUBLIC_URL set
to the full public MCP endpoint (HTTPS; loopback HTTP for development). Existing
OWUI keys remain valid in dual mode. OAuth mode rejects them only on this MCP endpoint.
One scope, hub:access, delegates the user's existing hub permissions including actions.
Do not advertise read-only access: existing tools lack reliable read/write classification.

Use short access tokens (10 minutes), rotating refresh tokens and grants (30 day maximum),
hashed opaque credentials, single-use authorization codes, PKCE S256, mandatory resource
binding, and consent protected by a browser cookie + server-side CSRF state. Verify the
stable OWUI account ID and current hub authorization on requests. Grant revocation ends
all its credentials immediately. Secrets and session cookies never go to assistants.

## Task 1: durable OAuth provider and protocol tests

Add a forward-only operational migration and storage/provider module. First write failing
integration tests using real FastMCP endpoints and temporary databases; mock only OWUI's
external session verification. Cover login, consent, deny, PKCE, resource/client binding,
code single use, refresh rotation/replay, restart persistence, account removal/replacement,
revocation, legacy/dual/oauth modes and unsafe configuration.
Run tests/test_mcp_oauth.py and existing tests/test_mcp_server.py after implementation.

## Task 2: route deployment and minimal browser pages

Wire optional settings and single-hub/gateway edge paths, including path-aware discovery.
Add login redirect to OWUI and Hubzoid consent plus a personal connections/revoke page.
Verify same-origin + CSRF protections and gateway metadata isolation with integration tests.
Use server-rendered responsive HTML; no admin privilege required to revoke one's own grants.

## Task 3: assistant connection instructions and verification

Document exact Claude Code and Claude web custom-connector setup and token transition.
Provide a thin Claude Code plugin template for endpoint configuration and usage instructions;
the MCP connection works without installing a plugin. Do not embed tokens in plugin files.
Run full pytest, review the whole diff with a fresh reviewer, address important findings,
and report remaining operational requirements. No deployment, package publishing or push.

## Review focus

Audit authorization boundary bypass, cross-hub credentials and discovery, browser CSRF,
redirect validation, concurrent exchange and refresh replay, account ID/email reassignment,
credential storage, and unintended changes to legacy behavior. Review tests beyond happy paths.
