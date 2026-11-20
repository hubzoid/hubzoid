# MCP connectors

[Model Context Protocol](https://modelcontextprotocol.io) servers give an agent
more tools. Add them in two ways: in the hub folder's `connectors/.mcp.json`,
where they reach everyone who can use the agent, or in Settings, where each
person connects their own account. To serve an agent to assistants such as
Claude Code instead, see the [hosted MCP server](mcp-server.md).

## Quick start

`<hub>/connectors/.mcp.json` uses the shape Claude Desktop uses:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GH_TOKEN}"}
    },
    "company": {
      "transport": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer ${COMPANY_TOKEN}"}
    }
  }
}
```

Set `GH_TOKEN` and `COMPANY_TOKEN` in the hub's `.env`. Hubzoid loads it before
the servers start.

## Reference

### Server fields

| Field | Default | What it does |
|---|---|---|
| `command`, `args` | | Starts a local server (stdio). |
| `env` | | Environment for a local server. Pass secrets here, not in `args`. |
| `url` | | The address of a remote server. |
| `transport` | `sse` when `url` is set, `stdio` otherwise | `http` (also `streamable-http`) for Streamable HTTP, or `sse`. |
| `headers` | | HTTP headers for a remote server. |
| `client_session_timeout_seconds` | `HUBZOID_MCP_TIMEOUT`, else 30 | Seconds one tool call may take on the OpenAI Agents and Codex runtimes. Not passed to Claude. |

`${VAR}` in any string field is replaced with the environment value. A server
whose address, headers, command or arguments use a variable that is not set
stays off until it is set, and Settings shows which one. A value in `env` may
be empty on purpose.

### Environment settings

| Setting | Default | What it does |
|---|---|---|
| `HUBZOID_MCP_TIMEOUT` | 30 | Seconds one MCP tool call may take, for hub servers and Settings connectors. |
| `HUBZOID_CONNECT_JOURNEY` | `true` | `false` removes `connect_account` and the connection link. |
| `HUBZOID_CONNECT_TTL` | 600 | Seconds a connection link stays valid, kept within 60 to 3600. |
| `HUBZOID_PUBLIC_URL` | unset | The address people open. Needed for redirect URIs and connection links whenever it is not this computer. |
| `HUBZOID_ALLOWED_ORIGINS` | unset | Other addresses people use, comma separated. |
| `HUBZOID_CONNECTOR_PRIVATE_HOSTS` | unset | Private hosts a connector's sign-in may use, comma separated. |
| `HUBZOID_RESTRICTED_SURFACES` | `owui,web,api,mcp,workflow,app` | Surfaces that may carry restricted tools and connectors. Add `whatsapp` for WhatsApp. |

### Timeouts

One tool call may take 30 seconds on the OpenAI Agents SDK and Codex runtimes,
for hub servers (stdio included) and Settings connectors alike. Change it for
the hub with `HUBZOID_MCP_TIMEOUT`, or for one server with
`client_session_timeout_seconds`, which wins.

The Claude runtime times calls in the `claude` CLI (`MCP_TOOL_TIMEOUT`, in
milliseconds). An explicit `HUBZOID_MCP_TIMEOUT` is passed on to it, unless
`MCP_TOOL_TIMEOUT` is already set. Without one, the CLI keeps its own default,
which is longer: Claude Code 2.1.296 let a 130-second call finish. A server's
`client_session_timeout_seconds` is not passed to Claude, so a slow server
works there without it.

### Example servers

| Server | Use case |
|---|---|
| `@modelcontextprotocol/server-filesystem` | Read files under a directory |
| `@modelcontextprotocol/server-github` | Issues, PRs, code search |
| `@modelcontextprotocol/server-postgres` | Read-only SQL queries |
| `@modelcontextprotocol/server-slack` | Read channels (requires a Slack token) |

See https://github.com/modelcontextprotocol/servers for more.

## Safety

Hubzoid does not limit what a server can do with its credentials. Give each
server credentials with only the scope it needs before adding it.

On `claude-local`, the agent sees only the MCP servers Hubzoid passes it: the
hub's own tools, the servers in its `.mcp.json`, the shared browser, and the
Settings connectors the current person may use. It never loads the connectors
of the Claude account the machine is signed in to (claude.ai Gmail, Drive,
Slack and so on) or MCP servers from that account's user or project settings.
`hub.call_llm` and the eval judge get no tools or servers at all. Hubzoid
refuses to start a Claude run with an SDK that cannot enforce this.

MCP credentials stay off the command line. On `claude-local`, the headers, URLs
and `env` of the hub's servers and of the person's own connections go in a new
file readable only by the account running Hubzoid (mode 0600), one per chat
turn. It holds only that person's connections and is deleted when the turn
ends, fails or is cancelled. Other accounts on the machine cannot see these
credentials in the process list. Limits:

* It does not protect against root, or against other processes running as the
  same account as Hubzoid. They can read the file while a turn runs, and each
  MCP server's own environment.
* If the Hubzoid process is killed outright (for example `kill -9`) during a
  turn, that turn's file stays in the system temp directory, still readable
  only by the same account, until the OS clears it.
* A secret in a stdio server's `args` is still visible in that server's own
  process arguments. Pass secrets to stdio servers in `env`.

## Connectors in Settings

Remote MCP servers can also be added in Settings or in deployment YAML files.
Hubzoid holds each person's connection, so every chat turn and workflow run
reaches the server as the person (or, with a shared key, as the company
account).

A connector is registered once for the deployment and offered in the agents
that list it. Its capability `connector_<id>` exists only in those agents, and
a turn uses a connector only when the agent offers it and the person holds the
capability there.

| Sign-in (`sign_in`) | Who connects | Grant shown as |
|---|---|---|
| Each person signs in (`oauth`) | Every person, with their own account | Connect <name> |
| Shared key (`shared`) | Nobody. One company key, sent as a header | Use <name> |
| No sign-in (`none`) | Every person switches it on once | Connect <name> |

The Connectors tab also lists the hub folder's own servers
(`connectors/.mcp.json`) read-only: name, kind (local command or URL) and the
environment variable names they use, with whether each is set. It never shows
a value, header, command argument or URL. Local command servers stay in the
file, because adding a command from a browser would let any administrator run
it on the server.

### Settings and files use the same definitions

Settings writes `connectors/<id>.yaml` beside the deployment's `hubzoid.yaml`
(or in the hub folder for a standalone deployment). You can edit that file by
hand too. Bridges reread it when it changes:

```yaml
version: 1
name: Company tools
url: https://mcp.example.com/mcp  # replace with your server
sign_in: oauth
hubs: [support]
enabled: true
```

Adding from an agent's Connectors tab includes that agent in `hubs`. It does
not grant people access: grant **Connect …** or **Use …** in the agent's
**Access** tab, then each person connects under **Settings → Connections**.

Client secrets and shared keys entered in Settings are saved in the deployment
`.env` with owner-only permissions and referenced from YAML as `env:NAME`. They
work without restarting the bridges and are never returned to the browser.

* Operator environment values win for hand-written references.
  Settings-generated references read their literal `.env` value, including
  after a restart.
* Replacing a key creates a new reference. Old values stay for the operator to
  clean up, in case another file still uses them.
* A stored client secret or shared key is never sent to a different server.
  Changing a connector's URL asks for it again (or, for a client secret, its
  removal), and a new client ID drops the old client's secret.
* Keep `.env` out of version control and protect its backups. On Windows the
  rewritten `.env` takes the deployment folder's permissions, so keep that
  folder restricted to the service account.

Edits in Settings keep the definition fields but may reformat the file and
remove YAML comments. A stale form is rejected if the file changed since it
opened. Repair malformed YAML before editing that connector in Settings.
Read-only deployments show a save error, and their operators manage connector
files outside Hubzoid.

### Add a connector

Organization administrators open **Settings → Agents → the agent → Connectors →
Add connector**. It registers the connector and offers it in that agent.

The list shows popular remote MCP servers (Linear, Notion, Atlassian, Sentry,
Stripe, GitHub, Gmail, Google Calendar, Drive, Docs, Sheets and others), each
with how people sign in: *Each person signs in*, *Needs an OAuth app* (create
an OAuth app at the provider with the redirect URI, then enter its client ID
and secret) or *No sign-in*. Choosing one fills its name, ID, address and
sign-in. **Custom server** starts empty. The list holds addresses only, checked
with Hubzoid's discovery on 2026-10-10 (Google on 2026-10-11). Run **Test**
before people use one.

| Field | What it does |
|---|---|
| **Name** and **ID** | The ID is a short slug such as `gmail`. It is also the capability `connector_<id>` and cannot change later. |
| **Server URL** | The MCP endpoint. HTTPS is required. Plain HTTP works only on this computer (`localhost`, `127.0.0.1`, `::1`). |
| **Sign-in** | *Each person signs in*, *Shared key* or *No sign-in*. A shared key goes in `Authorization: Bearer <key>` unless you name another header. Headers that would change the request, such as `Host`, `Cookie`, `Content-Length` or proxy headers, are refused. |
| **Client ID**, **client secret** | Optional. A client registered with the provider in advance. |
| **Scopes**, **allowed tools** | Optional. When editing, **Load from server** lists the saved server's tools. Save first after changing the URL or sign-in. |

Google's Workspace MCP servers are a Google Developer Preview. Each needs its
API enabled in Google Cloud and a Web application OAuth client. Their entries
fill in read-only scopes and, as allowed tools, the server's read-only tools.
Without scopes Hubzoid asks for every scope a server lists, which for Google
includes full access, and a write tool would fail without its scope. Hubzoid
asks Google for offline access, so a connection renews itself.

Other actions:

* **Offer an existing connector** offers one another agent already has.
* **Stop offering in <agent>** removes it and its grants from that agent only.
  People's connections and other agents stay.
* **Delete connector** removes it from every agent. Connected people lose their
  connections, revoked at the provider where it supports revocation.

Agent managers who are not organization administrators see the tab but cannot
change it.

### Test a connector

**Test** reads the server's metadata. It registers nothing and changes no
settings. It shows the authorization server, whether Hubzoid can register
itself, and the redirect URI for a provider that needs a client created in
advance: `<public origin>/oauth/connectors/<id>/callback`. It also lists the
server's tools and marks which reach agents when tools are allowed by name.

To list tools it uses the shared key, no credential for *No sign-in*, or for
*Each person signs in* your own connection, so connect your account first.
Like any use, that may refresh your connection's sign-in.

### Public address and private hosts

Set `HUBZOID_PUBLIC_URL` (and `HUBZOID_ALLOWED_ORIGINS` for other addresses)
whenever people reach Hubzoid at an address other than this computer. A
gateway reached at several addresses accepts each one already set in its
environment or as a hub's `HUBZOID_PUBLIC_URL`, so a sign-in returns to the
address it started on. List any other address in the gateway's
`HUBZOID_ALLOWED_ORIGINS`. Redirect URIs are built only on a configured address
or on localhost, never on the Host a request carries. With sign-in off, the
connection routes answer only requests addressed to this computer unless a
public address is set.

The server names its own authorization server and endpoints, so Hubzoid limits
where they may point. Public addresses are fine. A private, loopback or
link-local address is used only on the server's own host (an internal MCP
server and its own sign-in), anywhere on this computer when the server is on
this computer, or on a host you list:

```bash
# an internal identity provider on another host (names or addresses, comma separated)
HUBZOID_CONNECTOR_PRIVATE_HOSTS=sso.corp.example.com,10.20.0.15
```

Cloud metadata addresses (169.254.169.254 and the like) are never used, even
when listed. Names are checked on the addresses they resolve to, and Hubzoid
connects only to the addresses it checked. **Test** names a refused host. Each
operation (discovery, a code exchange, a refresh) has one overall time limit.

Changing a connector's URL or sign-in method removes everyone's connection to
it, so a token never goes to a server other than the one that issued it. A
shared key connector needs its key again for the same reason. Removing a
connector removes the connections too. Grants of its capability stay listed as
no longer available until you remove them.

### Connect your account

Each person connects from the link the agent sends (`connect_account`, see the
[connection journey](#connect-from-chat-connection-journey)), or from
**Settings → My connections** (`/account/connections`. The older
`/portal/connections` address opens it). Before anything happens, the link's
page shows the account, the agent, the server's host and any scopes the
connector requests. Both pages list each app with its status and the agents
that use it for the person. Disconnect asks first.

The HTTP API behind it (also under `/portal/api/connections`):

| Call | Result |
|---|---|
| `GET /api/connections` | Each switched-on connector: `connector_id`, `name`, `connected`, `status` (`ok`, `expired`, `error` or `none`), `connected_at`, `allowed`, `agents` (the agents that use it for the person) |
| `POST /api/connections/<id>/connect` | `{authorize_url}`. The browser goes there. |
| `DELETE /api/connections/<id>` | Revokes at the provider when it can, then removes the connection |

After the provider's consent page the browser returns to
`/oauth/connectors/<id>/callback`, which sends it on to the page the connect
call named (`return_to`) or to `/account/connections?connected=<id>`. A failure
goes to the same place with `?error=<code>`.

### How it works

* **Discovery**: the server's 401 challenge and RFC 9728 protected resource
  metadata, then RFC 8414 (or OpenID) authorization server metadata, with the
  issuer checked against where it was found.
* **Client**: the pre-registered one, else Hubzoid registers itself (RFC 7591)
  once per redirect URI and reuses that client for everyone.
* **Authorization**: PKCE S256, the RFC 8707 `resource` indicator when the
  server publishes resource metadata, and the RFC 9207 `iss` checked. The
  request is bound to the signed-in account, single use, and valid for 10
  minutes. Only a digest of its `state` is stored.
* **Tokens** are encrypted with the deployment key (`HUBZOID_SECRET_KEY` or the
  key file). They are refreshed 60 seconds before they expire, one refresh per
  connection at a time across the whole deployment, so a rotating refresh
  token is never spent twice. A refresh the provider refuses marks the
  connection **expired**, and the person reconnects. A provider that cannot be
  reached marks it **error**, and the next turn tries again.
* **Per turn**: only restricted surfaces, an agent that offers the connector,
  `connector_<id>` in that agent, the allowed tools, and never a server that
  would replace a hub MCP server.

### Runtimes

One per-turn source (`hubzoid/connectors/per_user.py`, reached through
`hubzoid/personal_mcp.py`) feeds every backend, with the same servers, rules
and allow-lists.

| Backend | How the servers join a turn |
|---|---|
| Claude (`claude-local`) | A per-turn copy of the SDK options with an `http` MCP spec per server |
| OpenAI Agents | Per-turn Streamable HTTP clients. Their tools join a per-turn clone of the agent |
| Codex (`codex-local`) | A per-turn copy of the tool registry. Hubzoid runs the MCP client and the Codex app-server only sees dynamic tools |
| Hosted MCP (`/mcp`) | Not a backend. Each `tools/list` lists the caller's apps as `my_<id>__<tool>`, and each call is forwarded with the same client and checks ([hosted MCP](mcp-server.md#connected-apps)) |

On the three backends tools are named `mcp__my_<id>__<tool>`, as Claude names
MCP tools. OpenAI accepts only letters, digits, `_` and `-`, up to 64
characters. So on OpenAI Agents and Codex, a tool name with other characters
(`issue.get`) has them replaced by `_` and ends in a short hash
(`mcp__my_jira__issue_get_bd577d`), and a longer name is cut and ends in a
hash. Two tools never end up with one name. Claude keeps names as the server
spells them.

Rules on every backend:

* The credential rides only in the MCP client's request header. It never
  enters the prompt, a log line or a tool result.
* The connector's tool allow-list applies.
* A connector never shadows a hub tool. Its tools are namespaced, so a clash
  can only be a server key. That server is skipped for the turn and a warning
  is logged.
* A server that cannot be reached is dropped for that turn. The turn goes on.
* Only the hub's main agent gets connectors. Sub-agents do not.
* Only surfaces in `HUBZOID_RESTRICTED_SURFACES` carry them. A shared Slack
  channel (`slack-channel`) never does, whatever that setting says.

### Connector capability (`connector_<id>`)

A turn uses a connector only when the caller holds `connector_<id>` in that
agent. Grant it in Settings like any other capability, in the **Connectors**
group of the Access drawer. If the access store cannot be read, no connector is
used that turn.

### Limits

* A provider that offers neither dynamic client registration nor a client
  registered in advance with `client_secret_basic`, `client_secret_post` or no
  secret (PKCE) cannot be connected. `private_key_jwt` is not supported.
* A server that publishes no OAuth authorization server metadata cannot be
  connected with OAuth.
* An access token without an expiry is not checked between turns. If the
  provider revokes it, the person reconnects.
* Through an outbound proxy from the environment (`HTTPS_PROXY`), the proxy
  makes the connection. Hubzoid checks the addresses a name resolves to on this
  computer, and leaves a name it cannot resolve to the proxy's own rules.
* A new authorization never keeps the refresh token of an earlier one, since
  nothing shows it is for the same remote account. A provider that issues a
  refresh token on the first consent only leaves a reconnect without one, and
  the connection then expires with its access token. Disconnect, then connect,
  to grant it again.
* Disconnecting cancels a sign-in that is still finishing. Its tokens are
  revoked, not saved.
* Provider-specific authorization parameters cannot be configured. Google is
  asked for offline access (`access_type=offline`, `prompt=consent`) on its own.
* There is no per-chat switch for a connector. A granted, connected connector
  is available in every chat with that agent.

## Connect from chat (connection journey)

On by default. A person asks the agent to connect an app ("connect my Gmail"),
or asks for something an outside app would do, in web chat or WhatsApp. The
agent sends a personal link in one short line. The person approves access in
the browser, a Hubzoid page shows the verified result, and WhatsApp gets a
confirmation. Called with no app, `connect_account(app, reconnect=false)` lists
what this person can connect in this agent, with each one's status.

An app is connectable when a switched-on connector with that ID is offered in
the agent ([Connectors in Settings](#connectors-in-settings)). A shared key
connector has nothing to connect, and the agent says so. The optional Composio
integration (`CONNECTIONS`, `COMPOSIO_API_KEY`) is separate from the journey.

A link needs:

* the `connector_<id>` capability for the person.
* the surface in `HUBZOID_RESTRICTED_SURFACES` (add `whatsapp` for WhatsApp).
* `HUBZOID_PUBLIC_URL` set to the public address people open. The link is
  `<public>/portal/connect/<id>`.

To turn it off, in the hub's `.env`:

```dotenv
HUBZOID_CONNECT_JOURNEY=false
```

### What happens

1. `connect_account` finds the connector, checks the capability (surface
   first) and whether the person is connected already. If they are, the agent
   says so. Otherwise it gets a link to `/portal/connect/<id>`, never a
   provider URL. The link records the person's account id.
2. The link page needs a signed-in session for that same account. A signed-out
   person signs in and comes straight back. Anyone else, including a new
   account with the same email, gets "This link is for another account",
   recorded in the access log.
3. **Continue** (a same-origin POST) checks again for a block or a revoked
   grant and sends the browser to the provider's consent page. A POST from any
   other origin, or with `Origin: null`, is refused.
4. The provider returns to `/oauth/connectors/<id>/callback`, which stores the
   tokens encrypted and sends the browser to `/portal/connect/<id>/done`. That
   page reads the result from Hubzoid's own records, never from the return URL.
5. The page says **connected** ("Close this tab and carry on in your chat"),
   **not connected** or **finishing** (it checks `/status` for up to 30
   seconds). WhatsApp gets its confirmation from the hub's inbound process (see
   [inbound surfaces](inbound-surfaces.md)).

A refused authorization ends the journey as not connected at once. Other
outcomes: **Cancel** on the page, an **expired** link (`HUBZOID_CONNECT_TTL`),
and a **newer link** for the same app, which stops the older one. Asking with
`reconnect=true` replaces the existing connection.

Limits:

* The WhatsApp confirmation and the YES continuation are WhatsApp only.
  Telegram and web chat get the link and the done page, then the person sends
  their next message.
* The person needs a chat-app account and signs in to it in the browser that
  opens the link, on a phone too.

### Check it with real accounts

Unit tests use fakes. Before relying on a provider, run once on a test
deployment:

1. Add the provider's MCP server in Settings (for example ID `gmail`). If it
   needs a client created in advance, register the redirect URI **Test** shows:
   `<public>/oauth/connectors/gmail/callback`.
2. Grant **Connect Gmail**. Add `whatsapp` to `HUBZOID_RESTRICTED_SURFACES` to
   try it there.
3. Ask "connect my Gmail". Open the link while signed in as another account
   (expect 403), then as the right one. Approve at the provider.
4. Expect the done page to say connected, and Settings → My connections to list
   it as connected.
5. Ask something that needs Gmail on each backend (`claude-local`, an OpenAI
   model, `codex-local`). The tool must answer with that person's mailbox.

## Coming from Open WebUI (1.x)

`hubzoid migrate openwebui` keeps each Open WebUI account id as the Hubzoid
account id, so connections made in 1.x carry over. Open WebUI's own External
Tool Servers are not moved. Add them as connectors in Settings. See
[upgrading](UPGRADING.md#upgrading-to-20-open-webui-removed).

## Troubleshooting

* **A hub server never starts.** A `${VAR}` it uses is not set. Settings shows
  which one.
* **A connection shows `expired`.** The provider refused a refresh. Reconnect.
* **A connection shows `error`.** The provider could not be reached. The next
  turn tries again.
* **Test refuses a host.** It is private and not listed in
  `HUBZOID_CONNECTOR_PRIVATE_HOSTS`, or it is a cloud metadata address.
* **A slow tool times out.** Raise `HUBZOID_MCP_TIMEOUT` or the server's
  `client_session_timeout_seconds`.

For deployment and upgrade checks, see [administration](ADMINISTRATION.md) and
[upgrading](UPGRADING.md).
