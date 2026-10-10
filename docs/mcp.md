# MCP connectors

[Model Context Protocol](https://modelcontextprotocol.io) servers attach as
additional tool sources to your agent. Configure them in
`<hub>/connectors/.mcp.json`.

## Format

The same shape Claude Desktop uses:

```json
{
  "mcpServers": {
    "<name>": {
      "command": "...",      // for stdio transport
      "args": ["..."],
      "env": {"VAR": "..."}
    },
    "<name2>": {
      "transport": "sse",    // for SSE transport
      "url": "https://...",
      "headers": {"Authorization": "Bearer ${TOKEN}"}
    }
  }
}
```

## Env-var interpolation

`${VAR}` references in any string field are resolved against the environment
at boot. Useful for tokens:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GH_TOKEN}"}
    }
  }
}
```

Set `GH_TOKEN` in your hub's `.env`; the platform loads it before MCP
startup.

## Useful servers

| Server | Use case |
|---|---|
| `@modelcontextprotocol/server-filesystem` | Read files under a directory |
| `@modelcontextprotocol/server-github` | Issues, PRs, code search |
| `@modelcontextprotocol/server-postgres` | Read-only SQL queries |
| `@modelcontextprotocol/server-slack` | Read channels (requires Slack token) |

See https://github.com/modelcontextprotocol/servers for the full list.

## Safety

Every MCP server is provisioned read-only by default (no writes, no posts).
Granting write access is a per-server decision. set up the server's
credentials with the right scope before adding to `.mcp.json`.

On `claude-local`, the agent sees only the MCP servers Hubzoid passes it: the
hub's own tools, the servers in its `.mcp.json`, the shared browser, and the
Console connectors the current person may use. It never loads the
connectors of the Claude account the box is signed in to (claude.ai Gmail,
Drive, Slack and so on) or MCP servers from that account's user or project
settings. `hub.call_llm` and the eval judge get no tools or servers at all.
Hubzoid refuses to start a Claude run with an SDK that cannot enforce this.

MCP credentials stay off the command line. On `claude-local`, the headers, URLs
and `env` of the hub's servers and of each person's own connections are written
to a new file readable only by the account running Hubzoid (mode 0600), one per
chat turn. That file holds only that person's connections and is deleted when the
turn ends, fails or is cancelled. Other accounts on the machine cannot see these
credentials in the process list. Limits:

- It does not protect against root, or against other processes running as the
  same account as Hubzoid. They can read the file while a turn runs, and each
  MCP server's own environment.
- If the Hubzoid process is killed outright (for example `kill -9`) during a
  turn, that turn's file stays in the system temp directory, still readable only
  by the same account, until the OS clears it.
- A secret written into a stdio server's `args` is still visible in that
  server's own process arguments. Pass secrets to stdio servers in `env`.

## Connectors in the Console (both UI modes)

Remote MCP servers are added in the Console, in the web app mode and in Open
WebUI mode alike, and Hubzoid holds each person's connection. Every chat turn
and workflow run reaches the server as the person (or, for a Shared key, as the
company account). Open WebUI's own MCP servers are not used: see
[Open WebUI mode](#open-webui-mode).

A connector is registered once for the deployment and offered in the agents
that list it. Its capability `connector_<id>` exists only in those agents, and
a connector is used in a turn only when the agent offers it and the person
holds the capability there.

Three kinds of sign-in:

| Kind | Who connects | Grant shown as |
|---|---|---|
| Each person signs in (`oauth`) | Every person, with their own account | Connect <name> |
| Shared key (`shared`) | Nobody: one company key, sent as a header | Use <name> |
| No sign-in (`none`) | Every person switches it on once | Connect <name> |

The Connectors tab also lists the hub folder's own servers (`connectors/.mcp.json`)
read-only: name, kind (local command or URL) and the environment variable names
they use, with whether each is set. Never a value, header, command argument or
URL. Those servers reach everyone who can use the agent. Local command servers
stay in the file, because adding a command from a browser would let any
administrator run it on the server.

### Add a server (organization administrators, in the Console)

**Console → Agents → the agent → Connectors → Add connector** registers it and
offers it in that agent. **Offer an existing connector** offers one another
agent already has. **Stop offering in <agent>** takes it out of that agent only
and removes its grants there; people's connections and other agents stay.
**Remove from every agent** deletes it:

- **Name** and **ID**. The ID is a short slug such as `gmail`. It is also the
  capability `connector_<id>` and cannot be changed later.
- **Server URL**: the MCP endpoint. HTTPS is required. Plain HTTP is accepted
  only on this computer (`localhost`, `127.0.0.1`, `::1`) for development.
- **Sign-in**: *Each person signs in*, *Shared key* (the key, and optionally
  the header it goes in: default `Authorization: Bearer <key>`; headers that
  would change the request, such as `Host`, `Cookie`, `Content-Length` or proxy
  headers, are refused), or *No sign-in* for a server that needs no account.
- Optional: a **client ID** and **client secret** registered with the provider
  in advance, **scopes**, and **allowed tools**.

**Test** reads the server's metadata and changes nothing. It shows the
authorization server, whether Hubzoid can register itself, and the redirect URI
to register with a provider that needs a client created in advance:
`<public origin>/oauth/connectors/<id>/callback`.

Set `HUBZOID_PUBLIC_URL` (and `HUBZOID_ALLOWED_ORIGINS` for other addresses)
whenever people reach Hubzoid at an address other than this computer. Redirect
URIs are built only on a configured address or on localhost, never on the Host
a request happens to carry. With sign-in off, the connection routes answer only
requests addressed to this computer unless a public address is configured.

The registered server names its own authorization server and endpoints, so
Hubzoid limits where they may point. Public addresses are fine. A private,
loopback or link-local address is used only on the registered server's own
host (an internal MCP server and its own sign-in), anywhere on this computer
when the server is on this computer, or on a host you list:

```bash
# an internal identity provider on another host (names or addresses, comma separated)
HUBZOID_CONNECTOR_PRIVATE_HOSTS=sso.corp.example.com,10.20.0.15
```

Cloud metadata addresses (169.254.169.254 and the like) are never used, even
when listed. Names are checked on the addresses they resolve to, and Hubzoid
connects only to the addresses it checked. The Console test names a refused
host. Each operation (discovery, a code exchange, a refresh) also has one
overall time limit.

Changing a connector's URL or sign-in method removes everyone's connection to
it, so a token is never sent to a server other than the one that issued it.
Removing a connector removes the connections too. Grants of its capability stay
listed as no longer available until you remove them. Agent managers who are not
organization administrators see the tab but cannot change it.

### Connect (each person)

From the link the agent sends (`connect_account`, the connection journey
below), or from the connections page: **Settings, Connections** in the web
app, `/portal/connections` in Open WebUI mode. The HTTP API behind it (also
under `/portal/api/connections`):

| Call | Result |
|---|---|
| `GET /api/connections` | each switched-on connector: `connector_id`, `name`, `connected`, `status` (`ok`, `expired`, `error` or `none`), `connected_at`, `allowed` |
| `POST /api/connections/<id>/connect` | `{authorize_url}`; the browser goes there |
| `DELETE /api/connections/<id>` | revokes at the provider when it can, then removes the connection |

After the provider's consent page the browser returns to
`/oauth/connectors/<id>/callback`, which sends it on to the page the connect
call named (`return_to`) or to `/account/connections?connected=<id>`. A
failure goes to the same place with `?error=<code>`.

### How it works

- **Discovery**: the server's 401 challenge and RFC 9728 protected resource
  metadata, then RFC 8414 (or OpenID) authorization server metadata, with the
  issuer checked against where it was found.
- **Client**: the pre-registered one, else Hubzoid registers itself (RFC 7591)
  once per redirect URI and reuses that client for everyone.
- **Authorization**: PKCE S256, the RFC 8707 `resource` indicator when the
  server publishes resource metadata, and RFC 9207 `iss` checked. The request
  is bound to the signed-in account, single use, and valid for 10 minutes.
  Only a digest of its `state` is stored.
- **Tokens** are encrypted with the deployment key (`HUBZOID_SECRET_KEY` or the
  key file, see `hubzoid/secretbox.py`). They are refreshed 60 seconds before
  they expire, one refresh per connection at a time across every process of
  the deployment, so a rotating refresh token is never spent twice. A refresh
  the provider refuses marks the connection **expired** and the person
  reconnects. A provider that cannot be reached marks it **error** and the next
  turn tries again.
- **Per turn**, the same rules as Open WebUI mode: restricted surfaces only, an
  agent that offers the connector, `connector_<id>` in that agent, the allowed
  tools, never a server that would replace a hub MCP server. In Claude the tools are named
  `mcp__my_<id>__<tool>`.

### Limits

- A provider that offers neither dynamic client registration nor a client
  registered in advance with `client_secret_basic`, `client_secret_post` or no
  secret (PKCE) cannot be connected. `private_key_jwt` is not supported.
- A server that publishes no OAuth authorization server metadata cannot be
  connected with OAuth.
- An access token without an expiry is not checked between turns. If the
  provider revokes it, the person reconnects.
- Through an outbound proxy from the environment (`HTTPS_PROXY`), the proxy
  makes the connection: Hubzoid checks the addresses a name resolves to on
  this computer, and leaves a name it cannot resolve to the proxy's own rules.
- A new authorization never keeps the refresh token of an earlier one, since
  nothing shows it is for the same remote account. A provider that issues a
  refresh token on the first consent only leaves a reconnect without one: the
  connection then expires with its access token. Disconnect, then connect, to
  grant it again.
- Disconnecting cancels a sign-in that is still finishing: its tokens are
  revoked, not saved.
- Provider-specific authorization parameters (for example Google's
  `access_type=offline`) cannot be configured yet.

## Open WebUI mode

Since 1.2 Open WebUI mode (`HUBZOID_UI=openwebui`) uses the same Console
connectors:

- An administrator adds them in the Console. The Connectors tab is the same in
  both modes. Open WebUI's admin role alone does not allow it: the Console's
  organization administrators do.
- A person connects from the agent's link or `/portal/connections`. Both
  check the Open WebUI session live and bind the connection to the person's
  Open WebUI account id. A new account that reuses an email is a different
  person and inherits nothing.
- On each turn the bridge uses the account id from Open WebUI's forwarded
  headers. Without it (`ENABLE_FORWARD_USER_INFO_HEADERS=false` on a gateway)
  no connector reaches the turn, and the gateway warns at start.
- The connector's OAuth return (`/oauth/connectors/<id>/callback`) goes to a
  bridge through the edge, with the same fallbacks as `/portal`. Open WebUI's
  own `/oauth/` routes are unchanged.
- Open WebUI's External Tool Servers are not used. The edge refuses saves to
  them (and to their OAuth client registration) with a message that points to
  the Console. `OWUI_NATIVE_MCP` is ignored.
- `hubzoid migrate openwebui` keeps each Open WebUI account id as the Hubzoid
  account id, so connections carry over to the web app.

Moving from Open WebUI's own MCP servers: see [UPGRADING.md](UPGRADING.md).

### Runtimes

One per-turn source (`hubzoid/connectors/per_user.py`, reached through
`hubzoid/owui_mcp.py`) feeds all three backends, with the same servers, rules
and allow-lists.

| Backend | How the servers join a turn | Tool names |
|---|---|---|
| Claude (`claude-local`) | Per-turn copy of the SDK options with an `http` MCP spec per server | `mcp__my_<id>__<tool>` |
| OpenAI Agents | Per-turn Streamable HTTP clients and a per-turn clone of the agent | the MCP tool name |
| Codex (`codex-local`) | Per-turn copy of the tool registry. Hubzoid runs the MCP client and the Codex app-server only sees dynamic tools | the MCP tool name |

Rules that hold on all three:

- The credential rides only in the MCP client's request header. It never
  enters the prompt, a log line or a tool result.
- The connector's tool allow-list applies.
- A connector never shadows a hub tool. On a name clash it is skipped for that
  turn and a warning is logged. Claude namespaces every server, so there the
  clash can only be a server key.
- A server that cannot be reached is dropped for that turn. The turn goes on.
- Only the hub's main agent gets connectors. Delegates do not.
- Only surfaces allowed to reach restricted tools (`HUBZOID_RESTRICTED_SURFACES`)
  carry them. A shared Slack channel (`slack-channel`) never does, whatever that
  setting says.

### Connector capability (`connector_<id>`)

A connector is used in a turn only when the caller holds `connector_<id>` in
that agent. Grant it in the Console like any other capability, in the
**Connectors** group of the Access drawer. If the access store cannot be read,
no connector is used that turn.

## Connect from chat (connection journey)

On by default. A person asks the agent to connect an app (for example "connect
my Gmail"), or asks for something an outside app would do, in web chat or
WhatsApp. The agent sends a personal link in one short line. The person
approves access in the browser, a Hubzoid page shows the verified result, and
WhatsApp gets a confirmation. Called with no app, `connect_account` lists what
this person can connect in this agent, with each one's status.

An app is connectable when a switched-on connector with that ID is offered in
the agent (see [Connectors in the Console](#connectors-in-the-console-both-ui-modes)).
The link page uses the chat app's sign-in (Hubzoid's, or Open WebUI's in Open
WebUI mode), **Continue** starts Hubzoid's own authorization, and the provider
returns straight to the done page. A refused authorization ends the journey as
not connected at once. A Shared key connector has nothing to connect, and the
agent says so. The optional Composio integration (`CONNECTIONS`,
`COMPOSIO_API_KEY`) is unchanged and is not part of the journey.

### Turn it off

In the hub's `.env`:

```dotenv
HUBZOID_CONNECT_JOURNEY=false
# HUBZOID_CONNECT_TTL=600   # link lifetime in seconds (60 to 3600)
```

The agent otherwise has a `connect_account(app, reconnect=false)` tool on every
backend. A link also needs:

- the `connector_<id>` capability for the person (a Console grant);
- the surface in `HUBZOID_RESTRICTED_SURFACES` (add `whatsapp` for WhatsApp);
- `WEBUI_URL` or `HUBZOID_PUBLIC_URL` set to the public address people open
  (the link is `<public>/portal/connect/<id>`).

### What happens

1. `connect_account` finds the connector, checks the capability (surface
   first) and whether the person is connected already. If they are, the agent
   says so. Otherwise it gets a link to `/portal/connect/<id>`, never a
   provider URL. The link records the person's account id.
2. The link page needs a signed-in session for that same account. A
   signed-out person is sent to sign in and comes straight back. Anyone else,
   including a new account with the same email, gets "This link is for another
   account" (recorded in the access log).
3. **Continue** (a same-origin POST) re-checks a block or a revoked grant and
   sends the browser to the provider's consent page. A POST from any other
   origin, or with `Origin: null`, is refused.
4. The provider returns to `/oauth/connectors/<id>/callback`, which stores
   the tokens encrypted and sends the browser to `/portal/connect/<id>/done`.
   That page reads the result from Hubzoid's own records, never from the
   return URL.
5. The page says **connected** ("Close this tab and carry on in your chat"),
   **not connected** or **finishing** (it checks `/status` for up to 30
   seconds). WhatsApp gets its confirmation from the hub's inbound process (see
   [inbound surfaces](inbound-surfaces.md)).

Other outcomes: **Cancel** on the page, an **expired** link (the TTL), and a
**newer link** for the same app (the older one stops working). Asking with
`reconnect=true` replaces the existing connection.

### Limits

- The WhatsApp confirmation and the YES continuation are WhatsApp only.
  Telegram and web chat get the link and the done page, then the person sends
  their next message.
- The person needs a chat-app account and signs in to it in the browser that
  opens the link, on a phone too.
- There is no per-chat on/off switch for a connector in Open WebUI. A granted,
  connected connector is available in every chat with that agent.

### Check it with real accounts (manual)

Unit tests use fakes. Before relying on a provider, run once on a test
deployment:

1. Add the provider's MCP server in the Console (for example ID `gmail`). If it
   needs a client created in advance, register the redirect URI the Console's
   **Test** shows: `<public>/oauth/connectors/gmail/callback`.
2. Grant **Connect Gmail**. Add `whatsapp` to `HUBZOID_RESTRICTED_SURFACES` to
   try it there.
3. Ask "connect my Gmail". Open the link while signed in as another account
   (expect 403), then as the right one. Approve at the provider.
4. Expect the done page to say connected, and `/portal/connections` (or
   Settings, Connections) to list it as connected.
5. Ask something that needs Gmail on each backend (`claude-local`, an OpenAI
   model, `codex-local`). The tool must answer with that person's mailbox.

For deployment and upgrade checks, see [administration](ADMINISTRATION.md) and
[upgrading](UPGRADING.md).
