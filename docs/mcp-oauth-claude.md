# Connect Hubzoid MCP to Claude

## What this upgrade changes

Open WebUI still authenticates the user. Hubzoid asks permission for an assistant
connection, then issues that assistant separate OAuth credentials. Passwords and
Open WebUI session tokens stay out of the assistant.

Flow: **Claude → Hubzoid OAuth → Open WebUI login (if needed) → Hubzoid consent → Claude.**
An existing Open WebUI session skips the login form, but never skips consent.

The minimal implementation uses the existing FastMCP and MCP SDK dependencies.
There is no additional identity service or frontend build. Hubzoid owns durable
provider state and enforces grants; the SDK supplies the OAuth protocol handlers.

## 1. Install this code on the Hubzoid server

These changes are on branch `feat/mcp-oauth-minimal`, not yet released to PyPI.
Use the checkout containing that branch. In your existing **Python 3.11 or 3.12**
Hubzoid environment, from the repository root:

```sh
python -m pip install -e .
```

Before restarting, back up the Hubzoid operational database. Startup applies the
additive `op_0008` migration. The repo's migration runner refuses a database from a
newer schema when running older code; rollback to an older SDK therefore requires
restoring the matching pre-upgrade backup.

## 2. Enable OAuth-only MCP

In your **hub folder's** `.env` (not the SDK repo's `.env`):

```dotenv
MCP_SERVER=true
WEBUI_AUTH=true
MCP_PUBLIC_URL=https://your-domain.example/mcp
```

For a gateway hub:

```dotenv
MCP_SERVER=true
WEBUI_AUTH=true
MCP_PUBLIC_URL=https://your-domain.example/b/your-hub-slug/mcp
```

Use the exact URL exposed by the edge, without a trailing slash. Every gateway
hub has its own setting. Keep existing access grants.
OAuth does not grant someone access to a hub they could not already use.

Restart your existing `hubzoid run <hub-folder>` or `hubzoid gateway ...` service.
Keep Open WebUI authentication enabled, its current database accessible to the
bridge, and the existing internal Open WebUI URL configured as for the Console.
Open WebUI login must be on the same public origin as the MCP endpoint.

Use trusted HTTPS for remote clients. Claude web must reach the server over the
internet. Loopback HTTP is allowed only for local Claude Code development.

The built-in edge routes discovery automatically. A custom reverse proxy must
forward these paths to the same bridge:

| Single hub | Gateway hub |
| --- | --- |
| `/mcp` and `/mcp/oauth/*` | `/b/<slug>/mcp` and `/b/<slug>/mcp/oauth/*` (strip `/b/<slug>`) |
| `/.well-known/oauth-protected-resource/mcp` | `/.well-known/oauth-protected-resource/b/<slug>/mcp` (keep path) |
| `/.well-known/oauth-authorization-server/mcp/oauth` | `/.well-known/oauth-authorization-server/b/<slug>/mcp/oauth` (keep path) |

Keep `/auth` and Open WebUI APIs routed to Open WebUI. Apply your normal public
request size and rate limits, especially to `/mcp/oauth/register` and `/authorize`.
Do not log Authorization headers, cookies, token request/response bodies or
consent query strings. Registration uses public clients (`none`) and PKCE S256.

Quick discovery check (replace the domain):

```sh
curl -fsS https://your-domain.example/.well-known/oauth-protected-resource/mcp
curl -fsS https://your-domain.example/.well-known/oauth-authorization-server/mcp/oauth
```

Both must return JSON with your public URL, not an Open WebUI HTML page.

## 3. Connect Claude Code

```sh
claude mcp add --transport http --scope user hubzoid https://your-domain.example/mcp
claude
```

For a gateway, substitute the `/b/<slug>/mcp` URL. Inside Claude, run `/mcp`, choose
Hubzoid and authenticate. The browser opens Open WebUI login if needed, followed by
Hubzoid consent. Choose **Allow connection**, then return to Claude.

Ask: “List the tools available from Hubzoid, then read its available knowledge.”
If a tool needs additional hub permissions, grant those through your existing
Hubzoid access controls; reconnecting cannot bypass them.

If you previously configured Hubzoid with a static `Authorization` header, remove
that old MCP configuration first (`claude mcp remove hubzoid --scope user` for a
user-scoped configuration), then add the OAuth connection without a header. This
removes only Claude's configuration; it does not delete the API key in Open WebUI.

## 4. Connect Claude web / desktop remote connector

In Claude, open **Customize → Connectors → + → Add custom connector**. Enter a
name and the full public MCP URL. Leave advanced client ID/secret fields empty:
Hubzoid supports dynamic registration of public OAuth clients. Add the connector,
connect, sign in through Open WebUI, and approve Hubzoid consent.

Organization administrators may need to enable the connector first. Availability
and menu labels depend on your Claude plan/version. This is the remote connector
flow; a Claude Code plugin folder is not needed here.

## 5. Revoke a connection

Open `https://your-domain.example/mcp/oauth/connections`, or the corresponding
`/b/<slug>/mcp/oauth/connections` URL. Sign in with Open WebUI and revoke an
assistant connection. Its access and refresh tokens stop working immediately.
Static Open WebUI API keys are rejected on MCP. The old `MCP_AUTH_MODE` switch
has been removed; setting it to `legacy` or `dual` cannot restore key access.
Existing API keys are not deleted from Open WebUI, but cannot connect here.
OAuth still uses short-lived access tokens internally; clients obtain these
through login and consent rather than asking users to paste a permanent key.

## Plugin location

An optional local Claude Code plugin is at `plugins/claude/hubzoid`. It holds only
MCP configuration. Set `HUBZOID_MCP_URL` and launch with `claude --plugin-dir ...`;
see its README. Installing a plugin still requires each user's login and consent.
The SDK code and hub data remain on your Hubzoid server. Marketplace publication
and a separate OpenAI/Codex plugin package are outside this minimal release.

## Scope and operational limits

- One scope: `hub:access`. It includes reading context and executing allowed
  actions. This version does **not** offer a read-only OAuth grant.
- Access tokens last at most 10 minutes; rotating refresh tokens and consent
  grants expire at 30 days. Reusing an old refresh token revokes that grant.
- Credentials are random opaque values stored as SHA256 digests in Hubzoid's
  operational database, isolated by public MCP URL. Changing that URL requires
  reconnecting. Client registrations expire after one year.
- Each call rechecks current OWUI account ID and existing hub authorization.
  Account deletion, replacement, suspension or email changes invalidate access.
- Only existing hosted MCP tools are exposed. This does not automatically export
  downstream MCP servers, chat uploads, model delegates or private credentials.
- The plugin is a local template. No Claude marketplace listing is published.
- Protocol tests run locally; confirm one real Claude login against your HTTPS
  deployment before rolling out to users.

Sources: [Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp),
[Claude plugin manifest](https://code.claude.com/docs/en/plugins-reference),
[FastMCP OAuthProvider](https://gofastmcp.com/servers/auth/full-oauth-server).
