# Minimal Open WebUI-backed MCP OAuth

The user requested implementation with minimal changes, preserving existing
API-key connections and using Open WebUI login before Hubzoid consent.

Use the installed FastMCP OAuthProvider and MCP SDK protocol handlers. Add durable
opaque credentials/grants to the existing operational store, two small browser
pages, and discovery routing through the existing edge. Default to legacy auth;
operators opt into dual mode to migrate without invalidating current API keys.

This replaces the earlier proposed external authorization-server sidecar for the
first release. The tradeoff is that Hubzoid maintains the provider's persistence,
consent, resource-binding and revocation logic. Those boundaries receive protocol,
store and browser verification. Use one honest hub:access scope: the current tool
registry cannot safely promise read-only access across arbitrary custom tools.

Implementation plan: [minimal plan](../docs/superpowers/plans/2026-09-29-mcp-minimal.md).
Operator and Claude guide: [connection guide](../docs/mcp-oauth-claude.md).
No production deployment, marketplace publication or default authentication change.
