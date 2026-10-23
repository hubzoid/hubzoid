---
name: hubzoid-embed
description: Put a Hubzoid agent's chat inside another app (an ERP, admin dashboard or portal), signed in by that app, with assistant-ui or CopilotKit. Use when someone asks Claude Code or Codex to embed Hubzoid in their app, add a Hubzoid chat to their React page, register an app in hubzoid.yaml, or let the agent's tools call their app's API or MCP server as the signed-in person.
---

# Embed a Hubzoid chat in an app

You help someone put a Hubzoid agent inside their own app. Two codebases are
involved: the Hubzoid deployment (its `hubzoid.yaml`) and their app (its server
and its page). Their app's API does not change.

**Done means**, in this order:

1. `hubzoid embed list` shows the app as "ready".
2. A person signed in to their app sends a message from the app's page and gets
   a streamed reply.
3. If tools should reach the app: a tool call answers with that person's own
   data, and a second person gets only theirs.
4. A page on another address is refused (its `Origin` isn't in `origins`).

## Where the facts are

`docs/embedding.md` from the same Hubzoid version (`hubzoid --version`) has the
steps, snippets, endpoints and error codes; `examples/embed-agui` is a working
app with both chat libraries. `hubzoid embed --help` is right for the installed
version. When they disagree with this skill, follow them and say what differed.

## Ask first

1. Does the app already sign people in with JWTs (Auth0, Clerk, Keycloak, Okta,
   its own)? Yes: mode 2 (no secret, no pass route). No: mode 1.
2. The app's web address(es), for `origins`.
3. Which agent(s) the app's people may use, and which tools.
4. Should tools act as the person in the app? Through a hub tool calling the
   app's API, or the app's MCP server added as a connector?
5. assistant-ui (headless, their own chat UI) or CopilotKit (a ready chat
   through its runtime on their server)?

## Steps

1. **Register** (mode 1): `hubzoid embed secret <id> --origin <address> --agent
   <agent> [--tool ...] [--token-to ...]`. Paste the printed entry under `apps:`
   in `hubzoid.yaml`. The secret is shown once: put it straight into the app's
   server settings (`HUBZOID_CLIENT_SECRET`), never into a file in either
   repository and never into your reply. Mode 2: write the entry from
   docs/embedding.md "Mode 2" with `issuer`, `audience`, `jwks_url` (or
   `public_key`) and `scopes.map` or `all_users: true`.
   Hosted deployments: see "Hosted" in docs/embedding.md (an env variable
   with `secret_ref: env:...` needs no shell).
2. **Check**: `hubzoid embed list` says "ready". If not, its State column says why.
3. **Pass route** (mode 1, the app's server): copy the Node or Python snippet
   from docs/embedding.md section 2 behind the app's own sign-in. `sub` is the
   app's user id; put the person's own API token in `app_token` only if tools
   need it.
4. **Chat** (the app's page): copy the assistant-ui or CopilotKit snippet from
   section 3. Every request gets a fresh pass. Add the options the person wants
   (history, approvals, page context, stop, delete) from "Options for your page".
5. **Tools as the person** (optional): a hub tool reads `app_token()`; or add the
   app's MCP server in Settings › Agents › Connectors › Custom server, "No
   sign-in", and list `connector:<id>` in `access.tools` and `token_to`.
6. **Verify** the "Done means" list with two different people of the app.

## Rules

- Never create Hubzoid accounts for the app's people; the app vouches for them.
- Never widen access to make something work: fix `access`, `token_to` or the
  pass instead, and say what you changed.
- Never put the secret, a pass or a person's token in a log, a commit or chat.
- Don't edit Hubzoid's code for this. If a step fails, show the error code and
  look it up in docs/embedding.md "Troubleshooting".
