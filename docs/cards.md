# Cards: tool results as MCP Apps

A hub tool can show its result as a **card**: a table, a report, the progress
of a piece of work, or an action waiting for approval. Cards use the
[MCP Apps](https://github.com/modelcontextprotocol/ext-apps) standard
(`ui://` resources, `text/html;profile=mcp-app`), so one card is written once
for native chat and for assistants that render MCP Apps. Every result also
comes as readable text, which is what the model sees and what any host
without cards shows.

Nothing changes until you add `mcp-apps.yaml`. A card never grants a tool
anything: every request a card makes is checked again on the server, and no
card can approve anything.

## Host support

| Host | Status |
|---|---|
| Native chat (this release) | Verified on the Claude runtime (table, report, progress and approval cards, paging, reload, a hostile custom card, a shared snapshot) and the Codex runtime (table). |
| Claude (claude.ai, Claude Desktop) | **Not verified.** Needs the hub's MCP server on a public HTTPS URL with sign-in (see [mcp-oauth-claude.md](mcp-oauth-claude.md)) and a Claude account that renders MCP Apps. |
| ChatGPT | **Not verified.** Needs a public HTTPS MCP URL and a ChatGPT account with developer mode connectors. |

Hosts without cards get the text. `hubzoid apps probe` shows what a client
with given capabilities would get; it is a protocol check, never a claim that a
host works.

## Set it up (builder)

Ask Claude Code or Codex, for example:

> Add a table card for the existing `order_summary` tool. Use the built-in
> table card, keep networking off, and don't change who can call the tool.
> Show me the diff, validate it, apply it and show the text fallback.

The files it writes:

```yaml
# mcp-apps.yaml
version: 1
enabled: true
builtin_cards: [table, report, progress, approval]
```

```yaml
# cards/order-summary.yaml
version: 1
id: order-summary
title: Synthetic order summary
binding:
  tool: order_summary
  output_schema: schemas/order-summary.json   # optional, a JSON Schema for the result
resource:
  builtin: table
visibility: [model, app]
allowed_tools: [order_summary]   # what the card may call: read tools only
```

Then, from the hub folder:

```bash
hubzoid apps validate . --check
```

```bash
hubzoid apps apply . --expected-revision absent
```

```bash
hubzoid apps invoke . order-summary --args-file fixtures/order-summary-input.json --idempotency-key demo-1
```

```bash
hubzoid apps inspect . <instance-id> --format text
```

`apply` makes the checked files the next active revision; the next apply
passes the revision it was based on (`--expected-revision 1`), and a stale one
is refused with `revision_conflict`. `hubzoid apps config get .` shows the
active revision, digest and cards. `hubzoid apps config set . /enabled false
--expected-revision <n>` turns cards off.

### What each built-in card expects

The bound tool returns JSON (a string or an object). Strings are text, never
markup.

| Kind | Result |
|---|---|
| `table` | `{"kind": "table", "title", "columns": [{"key", "label", "type"}], "rows": [...], "page": {"next_cursor", "total_rows"?}, "sources": [{"label", "url"?}]}`. Types are `string`, `number`, `boolean`, `date` (YYYY-MM-DD). At most 100 rows a page; a tool that takes a `cursor` argument gets "Next page". |
| `report` | `{"kind": "report", "title", "sections": [{"heading", "markdown"}], "sources": [...]}`. Paragraphs and `- ` lists. |
| `progress` | `{"kind": "progress", "work_item_id", "summary"?}`. The state is read from the work item each time, so refreshing never starts work. |
| `approval` | `{"kind": "approval", "operation_id", "digest", "summary"?}`: an operation waiting for approval ([work-items.md](work-items.md)). The card shows it and links to Hub; the decision is made on the work item page. |

A result that does not fit (not JSON, too big, a schema mismatch, a work item
the caller cannot see) keeps the tool's own text and logs why. The `question`
and `plan` cards wait for durable human requests and plan digests, and are
refused by validation until those exist.

### A custom card

`resource.file` names one standalone HTML file inside the hub, pinned by its
SHA-256 (quote it: `sha256: "…"`). Validation refuses a file outside the hub,
through a link, larger than `limits.html_bytes`, or changed since it was
pinned. A custom card runs under the same fixed CSP as the built-in ones: no
network, no outside assets, no frames, no forms.

### Limits

Lower only; the defaults are the ceilings: `html_bytes` 1 MiB, `result_bytes`
256 KiB, `rows_per_page` 100, `calls_per_minute` 60 and `concurrent_calls` 4
per card and person, `resource_timeout_seconds` 10 (1 to 30).

## What people see

A card shows inside the tool's entry in the reply. Around it, Hub's own chrome
(which the card cannot change) shows the tool, the kind of card and, for
progress and approval, the live state and **Open in Hub** or **Review in
Hub**. While the card loads, and if it cannot load, the text is shown instead.
A card from an older revision still shows its saved version but cannot call
tools; a revoked card shows its text. A shared or exported conversation shows
no live card.

## Operating it

* **Sandbox origin.** Cards render on a separate origin with no cookies. On
  loopback that is the other of `localhost` and `127.0.0.1` (same port). A
  hosted deployment sets `HUBZOID_APPS_SANDBOX_ORIGIN` (for example
  `https://cards.hub.example.com`, pointing at the same server); without it,
  native chat shows the text. No card content goes to a vendor host.
* **Who may do what.** Card settings need `manage_access` on the hub. A card's
  requests are for the person it was made for; anyone else gets "not found".
  Its tool calls run as that person through the normal access guard, and only
  read tools run. Refusals are written to the audit log (`apps.denied`).
* **Revoking.** `hubzoid apps revoke . <instance-id>` (or
  `POST /portal/api/hubs/{hub}/apps/instances/{id}/revoke`) stops a card at
  its next request.
* **Backups.** Revisions, card HTML (by hash) and instances live in the
  operational database (`hz_app_*`, migration `op_0054`) and are backed up
  with it. Old card HTML is kept while instances refer to it.

## API

Under `/portal/api/hubs/{hub}`, errors as `{"detail": {"code", "message"}}`:

| Route | Does |
|---|---|
| `GET /apps/config`, `POST /apps/validate`, `PUT /apps/config` | Active revision; check the files; apply (`expected_revision`, optional `config` for `mcp-apps.yaml`). |
| `POST /apps/invoke` | Run a card's tool as the caller (`Idempotency-Key`). |
| `GET /apps/instances`, `GET /apps/instances/{id}` | The caller's cards; one card's result, text, state and binding. |
| `POST /apps/instances/{id}/dispatch`, `POST /apps/host` | A card frame's request (`mcp-apps/read-resource`, `resources/read`, `resources/list`, `tools/call`). `/apps/host` names the card in `params.serverId`, for assistant-ui's remote host. |
| `POST /apps/instances/{id}/revoke`, `POST /apps/probe` | Revoke a card; how a client would get each card. |

On the hub's MCP server, card-bound tools carry `_meta.ui.resourceUri` and an
output schema, results carry `structuredContent`, `ui://` resources are read
only by callers who may use the tool, and `hub_app_status` (app-visible)
refreshes a progress or approval card for its requester.
