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

## 1. Register the app

Make the client secret (it is stored write-only and shown once):

```bash
hubzoid embed secret erpnext --hub ./finance
```

Then add the app to `hubzoid.yaml` (every agent picks up the change within a
few seconds; `hubzoid embed list` shows whether each app is ready):

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

- **Approvals**: `useAgUiInterrupts()` lists them and
  `useAgUiSubmitInterruptResponses()` answers:
  `submit([{ interruptId, status: "resolved", payload: { approved: true } }])`.
- **History**: give the runtime a `threadList` adapter backed by
  `GET /api/agui/threads` and `GET /api/agui/threads/{id}`, converting with
  `fromAgUiMessages(...).map(m => fromThreadMessageLike(m, generateId(), { type: "complete", reason: "unknown" }))`.
- **Rejoin after a reload**: when `GET /api/agui/threads/{id}` says
  `running: true`, return `unstable_resume: true` and give a `history.resume()`
  that reads `GET /api/agui/threads/{id}/run` and yields the growing message.

The example's [`assistant-ui.jsx`](../examples/embed-agui/src/assistant-ui.jsx)
has all three, about 60 lines.

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

**Your app's MCP server**, added in Settings as a connector, gets it as the
`token_header` header (default `Authorization: Bearer <token>`) on every call.
The MCP server must use that per-request token, not one fixed key, and must not
share cached results between people.

The token is sent only to the tools in `token_to`, kept in memory for the
reply, and replaced with `[redacted]` in tool output. (The Claude runtime keeps
it in its private config file for the reply's length.)

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
  `{approved: true | false}` (or `{decision}`) for an approval and
  `{answer}` for a question. The reply continues in that response.
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

## Troubleshooting

| You see | Why |
|---|---|
| 401 `unknown_app` | The token's `iss`/`aud` match no app in `hubzoid.yaml` |
| 401 `invalid_token` | Wrong signature, algorithm, times, or no user claim |
| 403 `no_access` | The pass (or the token's scopes) allow no agent here |
| 403 `wrong_origin` | The page's address isn't in the app's `origins` |
| 403 `app_disabled` | `enabled: false` |
| 503 `app_unavailable` | The secret isn't set (`hubzoid embed list`), or the JWKS URL can't be fetched |
| The tool says "no person's token" | The tool isn't in `token_to`, or the pass has no `app_token` |
