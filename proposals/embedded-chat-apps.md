# Hubzoid chat inside any app

## Problem
A team wants the Hubzoid agent inside its own app (an ERP such as ERPNext or
Odoo, or an admin dashboard) with a chat it designs itself. Today the only ways
in are the Hubzoid web app (its own sign-in) and personal API keys made by hand
for one person. There is no way for an app to say "this is Alice, signed in
here", so either everyone shares one key, or each person needs a Hubzoid
account and key. Tools then reach the app as one shared account, which shows
people data they cannot see in the app itself.

## Why now
Hubzoid 2.0 has one chat engine with stored history, approvals and a per-call
access check on every runtime. Builders asked to put that agent in the apps
their teams already use, with the app's own sign-in and permissions.

## What should happen
An administrator registers the app (`apps:` in `hubzoid.yaml`) and gets a
client id and a client secret. The app is limited to a set of agents and
tools. Two ways to sign a person in, both standard JWTs, as Agno and Mastra do:

1. **Hubzoid pass.** The app's server signs a short JWT with the client secret
   (any JWT library). It names the person (`sub`), the agent and tools they may
   use, and their own app token for the tools.
2. **The app's own JWT.** Hubzoid checks the app's existing JWT with the app's
   keys (JWKS URL, public key or shared secret). No server code.

The app's chat talks AG-UI (`/api/agui`), so assistant-ui and CopilotKit work
with their own packages. Hubzoid creates no account: chats are kept under a
private id per app user. The same person can list and reopen their chats and
rejoin a running reply. Approvals arrive as AG-UI interrupts. Tool calls to the
app's own tools carry the person's app token, so the app's permissions apply.
Hubzoid ships no client or server library; the docs have copy-paste snippets.

The full plan, with how Agno, Mastra, eve, Sim and LangChain do each part, is
`HubzoidAgent/ToolsAnalysis/embedded-chat-plan.md` (reviewed by Codex).

## Scope and non-goals
- Not in this change: Sim-style hosted chat pages in an iframe, anonymous public
  chat, Vue/Svelte hooks, AG-UI client-declared tools, a Settings screen to edit
  apps (they live in `hubzoid.yaml`, like other file-owned settings), a per-app
  spend scope.
- The ERPNext MCP server's per-person change lives in that project, not here.

## Open questions
- How long a pass may last by default (1 hour proposed).
- Whether a later release adds a Settings screen to register apps.

## Definition of done
- An app registered in `hubzoid.yaml` signs a person in with each mode; wrong,
  expired, cross-app and malformed tokens are refused.
- The person chats through AG-UI from assistant-ui and CopilotKit example pages,
  sees tool calls, approves a waiting action, reloads and rejoins, and reopens
  past chats; another person never sees them.
- The agent can use only the allowed agent and tools, on all three runtimes, and
  the app's tools receive the person's own token, which never appears in logs.
- Disabling the app stops its tokens and running replies.
- Unit tests, the full suite, an end-to-end run on the test hub with screenshots,
  and a Codex review pass.
