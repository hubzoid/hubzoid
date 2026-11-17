# Connected apps over hosted MCP

Plan, 12 November 2026, against `origin/main` 2.0.8 (`fc8b6363`). The reviewed plan is an HTML page
on how a connected app's tools would reach Claude Code through a hub (Darshika). Built on branch
`implement/mcp-connected-apps`; not merged.

## Problem

A person connects an app (a Settings connector, for example a CRM) to an agent. Web chat and
WhatsApp give them its tools on every turn. An assistant connected to the same agent's hosted MCP
endpoint (Claude Code, Claude) never does: the MCP server builds one tool list at startup and never
asks which apps the caller connected. It still offers `connect_account`, which answers "already
connected … They can use it now." The person connects, is told it worked, and the tools never
appear. Reproduced on 12 November with a shared-key GitHub connector: chat called the connector;
the hosted tool list had 58 tools and none of the connector's.

## Why now

Personal assistants over MCP are one of the three ways to use a hub (product brief), and connectors
are how a hub reaches a person's own systems. The gap makes the two contradict each other.

## What should happen

On every `tools/list` and `tools/call`, the hosted server asks the question chat asks
(`personal_mcp.per_user_servers`) and offers the caller's apps as `my_<id>__<tool>`. A call is
forwarded, as that person, only for an alias in their own catalog, after the same checks as a hub
tool and as chat: run admission and `check_run`, connector grant and allow-list, approval rules
evaluated with the server key and the app's own tool name, and an access-log row (no row, no call).
The result comes back as the app sent it. `connect_account` over MCP says to reconnect the
assistant, because the stateless endpoint sends no list-changed notice.

## Scope and non-goals

Tools only: no resources, prompts, sampling, elicitation or MCP Apps proxying. No list-changed
notifications or session infrastructure. No new runtime and no hub model call for forwarding
(`ask: auto` approval rules keep calling the decision model, as in chat).

## How it was built, and where it departs from the page

- New `hubzoid/mcp_connected_apps.py`; `mcp_server.py` middleware gains connected-app listing and
  an `on_call_tool`; the per-call admission moved into a shared `_admitted` helper;
  `tools/connect_tools.py` answers per surface.
- **Client choice:** the chat runtimes' own client and wrappers are reused
  (`runtime.personal_mcp_server`, `hitl.gate.guard_mcp_server`, `access.guard.checked_server`), so
  `connectors/server_tools.py` is unchanged. This is the reuse the page allowed for.
- **Output schema not forwarded.** The MCP server validates every non-error result against a
  declared output schema; the hub's own answers (an approval waiting) cannot match the app's. The
  app's structured content is still forwarded.
- `hitl/gate.py`, `personal_mcp.py` and `settings.py` unchanged (the page marked the last two
  optional; no off switch was added).
- Tests: the two-person cases are in `tests/test_personal_mcp_parity.py`, the single-person cases
  in `tests/test_mcp_server.py`, the wording in `tests/test_connect_journey.py`. Approval parity is
  tested end to end over hosted MCP rather than in `tests/test_hitl.py`; no client tests were
  needed because no client was written.

## Open questions

- A real app with per-person sign-in: its endpoint, authentication flow, two people's accounts, refresh and
  reconnect, a read and an approved write, denial after revocation. Not tested; the GitHub
  connector and in-process fakes do not establish them.
- Whether the access-log row per forwarded call (stricter than chat, which logs connector use
  only through grants) should apply to chat too.
- Whether an assistant other than Claude Code needs a different reconnect instruction.

## Definition of done

Over hosted MCP, two people each list and call only their own connected apps, concurrently; a
revoked grant, an allow-list, a hub tool of the same name, approval rules and an unwritable
access-log row each stop a call before it reaches the app; a broken app leaves the hub's tools
listed; `connect_account` over MCP says to reconnect. Then the open checks above with a real per-person app.
