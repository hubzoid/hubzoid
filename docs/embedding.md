# Hubzoid chat inside your app

Put a Hubzoid agent inside your own app (an ERP such as ERPNext or Odoo, an
admin dashboard, a portal), with a chat you design yourself. Each person chats
as themselves, signed in by your app, and the agent's tools reach your app with
that person's own access. Your app's API doesn't change.

Hubzoid speaks [AG-UI](https://docs.ag-ui.com), so the chat can be built with
[assistant-ui](https://www.assistant-ui.com) or [CopilotKit](https://www.copilotkit.ai)
using their own packages. Hubzoid ships no client or server library for this:
this page has the snippets. A complete working app is in
[`examples/embed-agui`](../examples/embed-agui/).

## How it works

1. You register your app in `hubzoid.yaml` and get a **client id** and a
   **client secret**. The app is limited to a set of agents and tools.
2. Your app's server gives the signed-in person a short **pass**: a JWT signed
   with the client secret. (Or, if your app already issues JWTs, Hubzoid checks
   those instead: see [Mode 2](#mode-2-your-apps-own-jwt).)
3. Your chat sends messages to Hubzoid's AG-UI endpoint with the pass.
4. Hubzoid checks it, runs the agent with only what the pass and the app allow,
   and sends the person's own app token to your app's tools.
5. The person's chats are kept by Hubzoid; they can list and reopen them.

No Hubzoid account is created for people who come from your app. Their chats
are kept under a private id made from your app and their user id, which never
matches a Hubzoid account or anyone's access. Name and email are labels only.

## Setup at a glance

| Step | Where | What |
|---|---|---|
| 1 | Hubzoid | `hubzoid embed secret <id> --origin <your app's address>`: makes the secret and prints the `hubzoid.yaml` entry and your server's settings |
| 2 | Hubzoid | Paste the entry under `apps:` in `hubzoid.yaml`; `hubzoid embed list` says "ready" |
| 3 | Your server | Set `HUBZOID_URL`, `HUBZOID_CLIENT_ID`, `HUBZOID_CLIENT_SECRET`; add the `/hubzoid/pass` route ([Mode 1](#2-mode-1-give-the-person-a-pass-your-server)) |
| 4 | Your page | Add the chat with assistant-ui or CopilotKit ([section 3](#3-the-chat-your-page)) |
| 5 | Optional | Give tools the person's access: a hub tool with `app_token()`, or your app's MCP server as a connector ([section 4](#4-your-apps-tools)) |

If your app already signs people in with JWTs, skip the secret and the pass
route: [Mode 2](#mode-2-your-apps-own-jwt) is one `hubzoid.yaml` entry.

With Claude Code or Codex, the [`hubzoid-embed` skill](../skills/embed/SKILL.md)
walks through these steps in your two codebases.

## 1. Register the app

Make the client secret. It is stored write-only and shown once, with the entry
to add to `hubzoid.yaml` and the settings for your app's server:

```bash
hubzoid embed secret erpnext --hub ./finance --origin https://erp.acme.com \
  --agent finance --token-to app_lookup
```

The command never edits `hubzoid.yaml`: add the entry yourself (or ask Claude
Code or Codex to). Every agent picks up the change within a few seconds, and
`hubzoid embed list` shows whether each app is ready. A full entry:

```yaml
apps:
  erpnext:                                   # the client id
    name: Acme ERP
    secret_ref: secret:deployment/app-erpnext
    origins: [https://erp.acme.com]          # pages that may call Hubzoid (CORS)
    access:                                  # the most this app may allow
      agents: [finance]
      tools: [app_lookup, read_knowledge, "connector:erp"]
    token_to: [app_lookup, "connector:erp"]  # tools that receive the person's app token
    max_lifetime: 3600                       # longest a pass may last, in seconds
```

Tool names are the agent's own tool names, `connector:<id>` for a connector
added in Settings, `mcp:<server>` for one of the hub's MCP servers, or `"*"`.
Without a `hubzoid.yaml`, put the same `apps:` section in `apps.yaml` in the
deployment folder.

Turning an app off (`enabled: false`) or removing it refuses its passes at once
and stops replies it started.

**Rotating the secret**: `hubzoid embed secret erpnext --rotate` makes a new one
and the old one stops working at once, so update your app's server right away.

**Hosted (Railway, Maritime, Docker)**: `hubzoid.yaml` is in your repository, so
add the entry there and deploy. For the secret, either run the command in the
container (`docker compose exec hubzoid hubzoid embed secret erpnext --hub /hub`),
or skip the shell: put any random value of 32 or more characters in a variable
(Railway **Variables**, for example `ERPNEXT_CLIENT_SECRET`) and point the app
at it with `secret_ref: env:ERPNEXT_CLIENT_SECRET`. A stored secret can also be
set in Settings or with `hubzoid secrets set` ([secrets](secrets.md)).

## 2. Mode 1: give the person a pass (your server)

Add one route behind your app's own sign-in. It signs a JWT with any JWT
library. Node (`jose`):

```js
import { SignJWT } from "jose";
const secret = new TextEncoder().encode(process.env.HUBZOID_CLIENT_SECRET);

app.get("/hubzoid/pass", requireSignIn, async (req, res) => {
  const pass = await new SignJWT({
    name: req.user.name,
    access: { agent: "finance", tools: ["app_lookup", "read_knowledge"] }, // optional: narrower than the app's limit
    app_token: req.user.apiToken,            // the person's own token for your API (optional)
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuer("erpnext")                    // the client id
    .setAudience("hubzoid")
    .setSubject(req.user.id)                 // your app's user id
    .setIssuedAt()
    .setExpirationTime("10m")
    .sign(secret);
  res.type("text").send(pass);
});
```

Python (PyJWT):

```python
import time, jwt

def hubzoid_pass(user):
    now = int(time.time())
    return jwt.encode({
        "iss": "erpnext", "aud": "hubzoid", "sub": user.id, "name": user.full_name,
        "access": {"agent": "finance", "tools": ["app_lookup", "read_knowledge"]},
        "app_token": user.api_token,
        "iat": now, "exp": now + 600,
    }, os.environ["HUBZOID_CLIENT_SECRET"], algorithm="HS256")
```

What a pass may say:

| Claim | Meaning |
|---|---|
| `iss` | the client id (required) |
| `aud` | `hubzoid` (required) |
| `sub` | your app's user id, as text (required) |
| `iat`, `exp` | issued and expiry times; at most `max_lifetime` apart (required) |
| `name`, `email` | labels shown in the chat (optional) |
| `access` | `{agent or agents, tools}`: narrows the app's limit, never widens it. Without `tools`, every tool the app allows; `tools: []` is no tools |
| `app_token` | the person's own token for your API, sent only to the tools in `token_to` |

A pass is signed, not encrypted: the browser can read it. Put in `app_token`
only a token the browser may see, or mint a narrow, short-lived one for it.

## Mode 2: your app's own JWT

If your app already signs people in with JWTs (Auth0, Clerk, Keycloak, Okta, your
own), Hubzoid can check them directly, with no server code:

```yaml
apps:
  portal:
    mode: jwt
    issuer: https://acme.eu.auth0.com/
    audience: https://hubzoid.acme.com
    jwks_url: https://acme.eu.auth0.com/.well-known/jwks.json   # or public_key: | PEM, or secret_ref
    user_claim: sub
    access: {agents: [finance], tools: [read_knowledge, app_lookup]}
    scopes:                                  # how access is decided: map the token's scope values
      claim: scope                           # a space-separated string or a list
      map:
        "chat:finance": {agents: [finance], tools: [read_knowledge, app_lookup]}
    # or instead of scopes:  all_users: true   (every signed-in user gets the maximum)
    token_to: [app_lookup]                   # these tools receive the person's JWT itself
```

A token without a mapped scope gets no access (the same rule as Agno). Several
apps may use one identity provider: the token's issuer and audience together
pick the app, and a token that would match two apps is refused.

## 3. The chat (your page)

Every request carries a fresh pass. AG-UI's `HttpAgent` takes a `fetch` for that.

### assistant-ui

```bash
npm install @assistant-ui/react @assistant-ui/react-ag-ui @ag-ui/client
```

```jsx
import { AssistantRuntimeProvider, fromThreadMessageLike, generateId } from "@assistant-ui/react";
import { HttpAgent } from "@ag-ui/client";
import { fromAgUiMessages, useAgUiRuntime } from "@assistant-ui/react-ag-ui";

const HUBZOID = "https://hub.acme.com/b/finance";        // the agent's address

async function withPass(url, init = {}) {
  const pass = await (await fetch("/hubzoid/pass")).text();
  return fetch(url, { ...init, credentials: "omit",
                      headers: { ...init.headers, Authorization: `Bearer ${pass}` } });
}

function Chat({ threadId }) {
  const agent = useMemo(() => new HttpAgent({ url: `${HUBZOID}/api/agui`, threadId, fetch: withPass }), [threadId]);
  const runtime = useAgUiRuntime({ agent, resumeTranscript: "appended" });
  return <AssistantRuntimeProvider runtime={runtime}>{/* your UI */}</AssistantRuntimeProvider>;
}
```

- **Approvals and questions**: `useAgUiInterrupts()` lists them and
  `useAgUiSubmitInterruptResponses()` answers:
  `submit([{ interruptId, status: "resolved", payload: { approved: true } }])`.
  A question (`reason: "question"`) is answered with `payload: { answer }`
  (its choices are in `metadata.hubzoid.options`), or skipped with
  `status: "cancelled"`.
- **History**: give the runtime a `threadList` adapter backed by
  `GET /api/agui/threads` and `GET /api/agui/threads/{id}`, converting with
  `fromAgUiMessages(...).map(m => fromThreadMessageLike(m, generateId(), { type: "complete", reason: "unknown" }))`.
- **Rejoin after a reload**: when `GET /api/agui/threads/{id}` says
  `running: true`, return `unstable_resume: true` and give a `history.resume()`
  that reads `GET /api/agui/threads/{id}/run` and yields the growing message.
- **Approvals still open after a reload**: put the thread's `interrupts` on the
  last assistant message, with status `{type: "requires-action", reason:
  "interrupt"}` and `metadata.custom.agui.interrupts`; `useAgUiInterrupts()`
  then shows them again.

The example's [`assistant-ui.jsx`](../examples/embed-agui/src/assistant-ui.jsx)
has all of these, and the options below.

### CopilotKit

CopilotKit's free path is its runtime on your server, with Hubzoid registered
as an AG-UI agent. The runtime adds the pass on every call, server side:

```js
import { CopilotRuntime, createCopilotRuntimeHandler } from "@copilotkit/runtime/v2";
import { HttpAgent } from "@ag-ui/client";

const runtime = new CopilotRuntime({
  agents: async ({ request }) => {
    const user = await yourSignedInUser(request);
    return { finance: new HttpAgent({ url: `${HUBZOID}/api/agui`,
                                      headers: { Authorization: `Bearer ${await hubzoidPass(user)}` } }) };
  },
});
const handler = createCopilotRuntimeHandler({ runtime, basePath: "/api/copilotkit" });
```

```jsx
import { CopilotChat, CopilotKit, useInterrupt } from "@copilotkit/react-core/v2";

<CopilotKit runtimeUrl="/api/copilotkit" agent="finance" useSingleEndpoint={false}>
  <Approvals />                              {/* useInterrupt({ render: ({ interrupt, resolve }) => … }) */}
  <CopilotChat threadId={`${user.id}:${crypto.randomUUID()}`} />
</CopilotKit>
```

Two things to do on your server, both in the example's
[`server.mjs`](../examples/embed-agui/server.mjs):

- **Keep threads per person.** CopilotKit's default runner keeps a thread's
  events in memory by thread id, for any caller. Start thread ids with the
  signed-in person (`${user.id}:…`) and refuse runtime requests for a thread that
  isn't theirs.
- Set `COPILOTKIT_TELEMETRY_DISABLED=true` if you don't want CopilotKit to send
  usage telemetry.

CopilotKit's direct-from-browser mode (`selfManagedAgents`) is part of its paid
tier, so it isn't the path documented here.

### Which one

assistant-ui is headless: you build the chat from its parts, and the browser
talks to Hubzoid directly. CopilotKit gives a ready chat and goes through its
runtime on your server. Both are free on the paths above, and both were tested
with chat, tools, approvals and history.

### Options for your page

- **What the person is looking at**: send it with each message as
  `forwardedProps: { hubzoid: { page: { doctype: "Sales Order", name: "SO-1001" } } }`
  (or AG-UI `context`). The agent gets it as labelled data, never as
  instructions, at most 4,000 characters, and it never changes access. The
  person's message in the history is shown without it.
- **Stop a reply**: `POST /api/agui/threads/{id}/stop`.
- **Delete a chat**: `DELETE /api/agui/threads/{id}`.
- **Show refusals**: a refused request answers with
  `{"detail": {"code", "message"}}` (see [Troubleshooting](#troubleshooting));
  show the message to the person.
- **Which agent**: one address per agent (`/b/<agent>`); the pass's
  `access.agent` must be one of the app's agents.
- **Look and feel** is your page's: Hubzoid sends only AG-UI events.

## 4. Your app's tools

The agent reaches your app with the person's own app token, so your API applies
its own permissions as usual.

**A tool in the hub** (`tools_local/app_lookup.py`) gets it with `app_token()`:

```python
from agents import function_tool
from hubzoid.embed import app_token

@function_tool
def app_lookup(query: str) -> str:
    """Look up records in the app for the signed-in person."""
    token = app_token()          # None unless this tool is in token_to and the reply is from your app
    r = httpx.get("https://erp.acme.com/api/resource/Sales Order",
                  params={"filters": query}, headers={"Authorization": f"Bearer {token}"})
    return r.text
```

**Your app's MCP server**, added as a connector, gets it as the `token_header`
header (default `Authorization: Bearer <token>`) on every call. Add it in
Settings › Agents › (the agent) › Connectors › Add connector › Custom server:
its URL, and "No sign-in" (the person's token comes from your app). It is saved
as `connectors/<id>.yaml`. Then list `connector:<id>` in the app's
`access.tools` and `token_to`. The MCP server must use that per-request token,
not one fixed key, and must not share cached results between people.

In mode 2 the token your tools receive is your app's own JWT, so your API
checks it as it already does.

The example app has both: `/api/orders` and an MCP server at `/mcp`, each
answering only with the orders of the person the token belongs to, and a
`/source` page listing every call it received.

The token is sent only to the tools in `token_to`, kept in memory for the
reply, and replaced with `[redacted]` in tool output (text and structured
results, on every runtime) and in Hubzoid's logs. (The Claude runtime keeps it
in its private config file for the reply's length.)

**Naming tools in the app's limit** (`access.tools`): a hub tool by its name, a
connector as `connector:<id>`, every tool of an MCP server in the hub as
`mcp:<server>`, a hand-over to another agent by its tool name, or `*`. A tool
from a server the reply doesn't know is never within a limit. Give MCP servers
names that aren't prefixes of each other joined by `__` (`erp` and
`erp__admin`): on the Claude runtime, a tool name two servers could own is
refused for app replies. While a reply holds a person's app token, the OpenAI
runtime sends no trace for it.

## 5. Endpoints

All take `Authorization: Bearer <pass or app JWT>`. Behind a gateway, prefix the
agent's address (`/b/<agent>`).

| Route | What |
|---|---|
| `POST /api/agui` | Run: an AG-UI `RunAgentInput`; answers with AG-UI events (SSE) |
| `GET /api/agui/threads` | This person's chats with this agent |
| `GET /api/agui/threads/{id}` | One chat as AG-UI messages, `running`, pending `interrupts` |
| `GET /api/agui/threads/{id}/run` | Rejoin the reply being written (SSE); 204 when none |
| `POST /api/agui/threads/{id}/stop` | Stop it |
| `DELETE /api/agui/threads/{id}` | Delete the chat |

- **Threads.** A thread id your client makes up maps to a chat in this person's
  own space; the ids `GET /threads` returns open the same chats. Nobody can
  reach someone else's chat by naming its id.
- **History is Hubzoid's.** From `messages` only the new user message is used.
  One reply at a time per chat: a second message while one runs answers 409.
- **Approvals** come as `RUN_FINISHED` with `outcome: {type: "interrupt",
  interrupts: [...]}`. Answer with a new run whose `resume` is
  `[{interruptId, status: "resolved" | "cancelled", payload}]`; the payload is
  `{approved: true | false}` (or `{decision}`, one of the interrupt's
  `allowedActions`) for an approval and `{answer}` for a question; anything
  else answers 422. `cancelled` rejects an approval and cancels a question.
  The reply continues in that response. If it couldn't continue
  (`not_continued`: the chat was busy or restarting), send the same answer
  again: the saved decision is kept and the reply continues with your fresh
  token. If the reply failed to start (`could_not_start`), the answer stands in
  the chat; send a new message to carry on.
- **Page context**: AG-UI `context`, or `forwardedProps.hubzoid.page`, is passed
  to the agent marked "data, not instructions" (4,000 characters at most) and is
  never used to decide access.
- Client-declared AG-UI `tools` are ignored in this release, and messages are
  text only.

## Security

- Your app vouches for the person. Keep the client secret on your server; the
  browser only ever sees short passes.
- Only the app's `origins` may call from a browser (CORS without credentials);
  a request with an `Origin` that isn't the app's is refused. These routes never
  read a Hubzoid cookie, and a request with both is refused.
- A pass opens only the routes above: never Settings, People, keys or the web
  app's own API.
- Refused tokens are rate-limited per client address; runs are rate-limited per
  person.

## How others do it

| | Signing a person in | Access | Tools as the person | UI |
|---|---|---|---|---|
| Agno | JWT checked with your keys (`AuthorizationConfig(verification_keys)`) | scopes in the token | extra claims to tools (`dependencies_claims`), per-run MCP headers | AG-UI |
| Mastra | auth providers (`MastraJwtAuth`, Clerk, Auth0) | your code | per-caller connections | `@mastra/react` |
| eve | `jwtHmac`, `jwtEcdsa`, `oidc` | your code | per-caller connection auth | `useEveAgent` hooks |
| LangChain | your `@auth.authenticate` function | `@auth.on` rules | your code | their SDK |
| Hubzoid | a pass signed with the client secret, or your app's own JWT | the pass within the app's limit | the person's app token to `token_to` tools | AG-UI |

Registering an app: Agno sets one JWT verifier for the whole AgentOS in code
(`AgentOS(authorization=True, authorization_config=...)`) or with
`JWT_VERIFICATION_KEY`/`JWT_JWKS_FILE`; its YAML config covers other settings,
not sign-in. Its service accounts are made through an API. Mastra and eve set
their auth in code (`new Mastra({ server: { auth } })`, eve's `auth: [...]`).
Sim sets a deployed chat's sign-in in its UI. Hubzoid keeps one entry per app
in `hubzoid.yaml`, beside the deployment's other settings, and the secret in
the secret store (`hubzoid embed secret`).

## Troubleshooting

| You see | Why |
|---|---|
| 401 `unknown_app` | The token's `iss`/`aud` match no app in `hubzoid.yaml` |
| 401 `invalid_token` | Wrong signature, algorithm, times, or no user claim |
| 403 `no_access` | The pass (or the token's scopes) allow no agent here |
| 403 `wrong_origin` | The page's address isn't in the app's `origins` |
| 403 `app_disabled` | `enabled: false` |
| 400 `ambiguous_credentials` | The request carried a Hubzoid sign-in cookie as well as the pass; send `credentials: "omit"` |
| 503 `app_unavailable` | The secret isn't set (`hubzoid embed list`), or the JWKS URL can't be fetched |
| 401 `token_expired` | Get a fresh pass for each request (the examples do) |
| 429 `too_many` | Too many messages from one person in a minute, or too many refused tokens from one address |
| `app_token()` returns None | The tool isn't in `token_to`, or the pass has no `app_token` |
| A connector's tools are missing | `connector:<id>` isn't in `token_to` (and the connector has no shared key), or isn't in the app's `access.tools` |
| "not one of the tools this app allows" | The tool is outside the app's limit, the pass's `access.tools`, or (mode 2) the token's mapped scopes |
| 409 `run_in_progress` | A reply is being written or starting in this chat; rejoin or stop it |
| 422 `decision_required` | A resume without a clear `{approved: true/false}` |
