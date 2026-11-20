# Connect Hubzoid MCP to Claude

This guide connects Claude Code or Claude web to an agent's
[hosted MCP server](mcp-server.md). The person signs in to Hubzoid as for chat
and approves the connection. Hubzoid then gives that assistant its own OAuth
credentials. Passwords and the web app's session never reach the assistant.

Flow: **Claude → Hubzoid OAuth → Hubzoid sign-in (if needed) → Hubzoid consent → Claude.**
An existing sign-in skips the form, never the consent.

```sh
claude mcp add --transport http --scope user hubzoid https://your-domain.example/mcp
```

## Reference

| Item | Value |
|---|---|
| Scope | `hub:access` only. It covers reading context and running allowed actions. There is no read-only grant. |
| Access token | A signed JWT, valid for at most ten minutes ([details](mcp-server.md#access-tokens-and-outside-verifiers)) |
| Refresh token and consent | Expire after 30 days. Each refresh replaces the refresh token. Reusing an old one revokes the connection. |
| Client registration | Dynamic, public clients (`none`) with PKCE S256. A registration expires after one year. |
| Stored credentials | Refresh tokens, codes and grants are random opaque values, stored as SHA256 digests in the operational database and kept apart by public MCP URL. |
| Revocation page | `/mcp/oauth/connections`, or `/b/<slug>/mcp/oauth/connections` in a gateway |

## Enable OAuth MCP

In the **hub folder's** `.env`:

```dotenv
MCP_SERVER=true
HUBZOID_AUTH=true
MCP_PUBLIC_URL=https://your-domain.example/mcp
```

For a gateway hub, put `HUBZOID_AUTH=true` in the gateway's environment and
this in the hub's `.env`:

```dotenv
MCP_SERVER=true
MCP_PUBLIC_URL=https://your-domain.example/b/your-hub-slug/mcp
```

Use the exact URL the edge exposes, without a trailing slash. Each gateway hub
has its own setting. Restart `hubzoid run <hub-folder>` or the
`hubzoid gateway` service.

The sign-in page must be on the same public origin as the MCP endpoint. Use
trusted HTTPS for remote clients. Claude web must reach the server over the
internet. Loopback HTTP is for local Claude Code development only.

## Route a custom reverse proxy

The built-in edge routes everything. A custom reverse proxy must send these
paths to the same bridge:

| Single hub | Gateway hub |
| --- | --- |
| `/mcp` and `/mcp/oauth/*` | `/b/<slug>/mcp` and `/b/<slug>/mcp/oauth/*` (strip `/b/<slug>`) |
| `/.well-known/oauth-protected-resource/mcp` | `/.well-known/oauth-protected-resource/b/<slug>/mcp` (keep the path) |
| `/.well-known/oauth-authorization-server/mcp/oauth` | `/.well-known/oauth-authorization-server/b/<slug>/mcp/oauth` (keep the path) |

Keep `/auth` and `/api/auth/*` routed to Hubzoid as for chat. Apply your normal
request size and rate limits, especially to `/mcp/oauth/register` and
`/authorize`. Do not log Authorization headers, cookies, token request or
response bodies, or consent query strings.

Check discovery (replace the domain):

```sh
curl -fsS https://your-domain.example/.well-known/oauth-protected-resource/mcp
curl -fsS https://your-domain.example/.well-known/oauth-authorization-server/mcp/oauth
```

Both must return JSON with your public URL, not an HTML page.

## Connect Claude Code

```sh
claude mcp add --transport http --scope user hubzoid https://your-domain.example/mcp
claude
```

For a gateway, use the `/b/<slug>/mcp` URL. Inside Claude, run `/mcp`, choose
Hubzoid and authenticate. The browser opens the Hubzoid sign-in page if
needed, then Hubzoid consent. Choose **Allow connection** and return to Claude.

Try: "List the tools available from Hubzoid, then read its available
knowledge." If a tool needs more access, grant it in Settings. Reconnecting
cannot bypass a grant.

If Hubzoid was configured earlier with a static `Authorization` header, remove
that configuration first (`claude mcp remove hubzoid --scope user` for a
user-scoped one), then add the OAuth connection without a header.

## Connect Claude web or desktop

In Claude, open **Customize → Connectors → + → Add custom connector**. Enter a
name and the full public MCP URL. Leave the client ID and secret fields empty,
because Hubzoid registers public OAuth clients itself. Add the connector,
connect, sign in to Hubzoid and approve the connection.

Organization administrators may need to enable the connector first. Menu
labels and availability depend on your Claude plan and version.

## Revoke a connection

Open `https://your-domain.example/mcp/oauth/connections`, or the
`/b/<slug>/mcp/oauth/connections` URL in a gateway. Sign in and revoke the
assistant connection. Its tokens stop working on the next call.

Static API keys, including Open WebUI (1.x) keys, are refused on MCP, and the
old `MCP_AUTH_MODE` switch is gone.

## The Claude Code plugin

The `hubzoid` Claude Code plugin (`plugins/hubzoid`) builds and runs a Hub
([quickstart](quickstart.md)). It does not hold a connection to a hosted Hub,
because each Hub has its own MCP address. Connect with `claude mcp add` as
above. The old local plugin template at `plugins/claude/hubzoid` was removed.

## Limits

* Each call rechecks the account and its access to the agent. Deleting,
  replacing or suspending an account, or changing its email, ends its
  connections.
* Changing `MCP_PUBLIC_URL` means every assistant reconnects.
* The hub's own `connectors/.mcp.json` servers and people's private credentials
  are not exposed. Connected apps, uploads and sub-agents work as listed in
  [the MCP server page](mcp-server.md#what-an-assistant-can-do-next-to-chat).
* Protocol tests run locally. Confirm one real Claude login against your HTTPS
  deployment before rolling out to people.

## Troubleshooting

* **Discovery returns HTML.** The proxy does not send the `.well-known` paths
  above to the bridge.
* **The sign-in page loops or opens on another host.** The sign-in page and the
  MCP endpoint must share one public origin. Check `MCP_PUBLIC_URL` and
  `HUBZOID_PUBLIC_URL`.
* **Claude web cannot connect.** The server must be reachable over the internet
  with trusted HTTPS.

Sources: [Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp),
[FastMCP OAuthProvider](https://gofastmcp.com/servers/auth/full-oauth-server).
