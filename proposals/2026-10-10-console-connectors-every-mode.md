# MCP connectors in the Console, in every UI mode

Issues #15, #39, #40 and #41. Approved by the founder on 10 October 2026.

## Problem

MCP servers are set up in three places. In the web app, administrators add
connectors in the Console and Hubzoid holds each person's tokens. In Open
WebUI mode the Console list is read-only: servers are registered in Open
WebUI's admin screen with `OWUI_NATIVE_MCP=true`, every server shows on every
agent, and Hubzoid ignores Open WebUI's on/off switch and access list (#39).
Hub-wide servers in `connectors/.mcp.json` are not shown in the Console at
all. An administrator has to know which mode a deployment runs before adding a
connector, and moving between modes means registering everything again.

## Why now

Connectors are central to making a Hub useful to a team, and #39 is an access
bug in the mode a live deployment runs. Nobody uses Open WebUI MCP in
production yet (Ishangam on IRS is a test setup), so a fresh setup costs
little now and more later.

## What should happen

- Administrators add every remote MCP server in the Console, the same screen in
  both UI modes. A connector is registered once and offered per agent. People
  need the `connector_<id>` grant in that agent.
- Three sign-in kinds: each person signs in (OAuth), Shared key (one company
  key, sent as a header, nobody connects) and no sign-in.
- People connect from the link the agent sends (`connect_account`, on by
  default; with no app it lists what they can connect) or from their
  connections page (`/portal/connections` in Open WebUI mode, Settings,
  Connections in the web app). Nothing is injected into Open WebUI.
- Hubzoid stores servers and connections in its own database. In Open WebUI
  mode a connection belongs to the verified Open WebUI account id, so a new
  account that reuses an email inherits nothing, and `hubzoid migrate
  openwebui` carries connections over to the web app.
- The Console lists the hub folder's `.mcp.json` servers read-only, without any
  value. Local command servers stay in the file.
- Hubzoid stops reading Open WebUI's MCP servers, and the edge refuses saves to
  Open WebUI's External Tool Servers. `OWUI_NATIVE_MCP` is ignored.
- The Access drawer gets a Connectors group, and Hubzoid tools move last.
- Screens follow the UX principles in the plan: few words, details behind a
  "?" that opens on hover, tap or keyboard, plain words, always a next step.

## Scope and non-goals

- No import of Open WebUI servers or connections. Administrators add the
  servers again and people connect once.
- No editing of `.mcp.json` from the Console.
- No menu entry injected into Open WebUI.
- Tool names stay as they are on each runtime (Claude namespaces them, OpenAI
  Agents and Codex use the bare name). Making them identical changes names
  that prompts rely on and needs its own proposal.

## Open questions

- Logos on the connect pages: the pages show the agent's name only.
- A Console notice when a gateway turns identity forwarding off: the gateway
  warns at start only.

## Definition of done

- In Open WebUI mode, a connector added in the Console is granted, connected
  through the link and used in an Open WebUI chat turn on all three runtimes.
- A link opened by another account, even with the same email, is refused. A
  shared Slack channel never gets a connector, whatever
  `HUBZOID_RESTRICTED_SURFACES` says.
- A Shared key is sent as its header, never shown again and never logged.
- Open WebUI's tool-server saves are refused at the edge.
- `docs/mcp.md` describes one setup and `docs/UPGRADING.md` the move.
- The plan with diagrams and the Codex review:
  `HubzoidAgent/docs/hubzoid-mcp-connectors-console-plan-2026-10-10.html`
  (product workspace, not in this repository).
