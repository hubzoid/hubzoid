# Cards: structured tool results in native chat, Claude and ChatGPT

Status: implementation authorized by the owner on 18 October 2026, for 2.0
(HUB-102, "Approved in full", built last in milestone D after milestone A).

## Problem

A hub tool can only answer in text. A table of orders, a weekly report, the
progress of a piece of work or a pending approval all arrive as text the
model retypes, and the chat cannot show them as what they are. The tool
result itself never reaches the chat: `ToolResult` carries no payload and the
message stream stores `{"status": "ok"}`.

## What should happen

Use the MCP Apps standard (stable 2026-01-26) so a card is written once and
works in native chat and in assistants that render MCP Apps. Keep it thin:
FastMCP serves the resources, assistant-ui renders them, Hubzoid owns only the
binding, the checks and the sandbox page.

1. **Optional files.** `mcp-apps.yaml` turns cards on and lowers limits;
   `cards/<id>.yaml` binds one tool to a card: a built-in kind (`table`,
   `report`, `progress`, `approval`) or a reviewed HTML bundle pinned by
   SHA-256. Strict YAML with file, pointer and line in every error. No file
   means no cards and no change.
2. **One wrapper on the bound tool, the same for every runtime.** After the
   guarded tool returns, its result is checked against the card's JSON Schema
   and kind rules, bounded (256 KiB, 100 rows), stored as an instance, and the
   model gets readable text made from the same data. Anything that does not
   fit gives the original text and a diagnostic, never a failed tool.
3. **The result reaches the chat.** The runtime adapters attach the card to
   `ToolResult`; the message stream stores it on the tool part as an MCP
   `CallToolResult` (text, `structuredContent`, instance id). Reload and
   replay show the same card without running the tool.
4. **Native chat renders with assistant-ui's `McpAppRenderer`** through one
   authenticated dispatch route per hub. Only four methods are admitted:
   `mcp-apps/read-resource`, `resources/read`, `resources/list` (the
   instance's own resource) and `tools/call` (the card's allowed tools, read
   class only, run as the signed-in person through the normal guards).
   Host chrome outside the frame shows the tool, its source and, for an
   approval, the authoritative operation facts with "Review in Hub".
5. **A deployment-owned sandbox origin.** assistant-ui's default frame loads
   a vendor host (`scf.auiusercontent.com`). The portal swaps that one module
   for a Hubzoid frame with the same interface, served from a separate
   cookie-free origin (loopback: the other of `localhost` and `127.0.0.1`;
   hosted: `HUBZOID_APPS_SANDBOX_ORIGIN`). Card HTML runs in an inner
   `allow-scripts` frame with an opaque origin and a fixed CSP: no network,
   no external assets, no nested frames, no forms.
6. **External hosts through the MCP server.** Card-bound tools publish
   `_meta.ui.resourceUri`, visibility and an output schema; `ui://` resources
   are served with `text/html;profile=mcp-app` and an empty-domain CSP, read
   only by callers who may use the tool. Results carry `structuredContent`.
   `hub_app_status` (app-visible) refreshes a progress or approval card for
   the instance's own requester.
7. **Decisions stay in Hub.** No card can approve. The approval card shows the
   HUB-93 operation bound to its digest and links to the authenticated work
   item page, where the existing approve control checks identity and digest.
8. **Files, CLI and API for the same things:** `hubzoid apps config get/set`,
   `validate`, `apply` (revision compare-and-set), `list`, `invoke`,
   `inspect`, `dispatch`, `revoke`, `probe`; the same under
   `/api/hubs/{hub}/apps/...`.

## Scope and non-goals

- The `question` and `plan` cards need HUB-36's durable human requests and
  HUB-89's plan digests, which are not built. They are rejected by validation
  with that reason until those land.
- No widget messages into the thread, no model-context updates, no display
  modes beyond inline, no network-connected custom cards.
- External cards call ordinary tools through their host, which are the same
  governed MCP calls the model could make; an instance-scoped dispatch tool
  for external hosts waits until a card needs instance-scoped actions.
- Host support is claimed only with evidence. Claude and ChatGPT need an
  account and a public HTTPS endpoint; without them they are recorded as
  blocked, not passed.

## Definition of done

- Each built-in card shows truthful empty, loading, error and done states,
  with the same text for the model and for hosts without cards.
- The card survives Claude, Codex and OpenAI Agents runs, reload and replay.
- Resources have the exact MIME type, a fixed CSP and a version-bound URI,
  and no card content reaches a vendor origin.
- Every widget read and call rechecks the person, the instance and the tool
  guard; a revoked instance or a write-class tool is refused.
- An approval card cannot settle an operation.
- Native chat verified first, then Claude, then ChatGPT, each with evidence
  or an explicit blocker.
