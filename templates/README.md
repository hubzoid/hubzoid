# Templates

Six complete hubs for common internal roles. Each one runs as-is on sample
data, so your team can see the agent work before any system is connected.
Where a template needs a real system (ledger, ERP, warehouse, alerting), it
uses a clearly marked placeholder tool in `tools_local/`: a small typed
Python function returning sample data. The buyer wires the real system in
that one file and keeps the signature. Nothing else in the hub changes.

| Template | Who on the team uses it | Agent type | Surfaces |
|---|---|---|---|
| [`morning-briefing`](morning-briefing) | Leadership: MD, finance head, operations head | Background, with Q&A | Web, Slack, Telegram |
| [`accounts-desk`](accounts-desk) | Accounts team, finance head | Tool, with Background | Web, Slack |
| [`supplier-slip-check`](supplier-slip-check) | Warehouse leads, operations head | Background, with Q&A | Team chat post, Slack, WhatsApp |
| [`stock-drift-watch`](stock-drift-watch) | Inventory controller, warehouse leads, operations head | Background, with Q&A | Web, Slack, WhatsApp |
| [`company-qna`](company-qna) | Everyone on the team | Q&A | Slack, Telegram, web |
| [`it-ops-digest`](it-ops-digest) | On-call engineer, IT-ops lead, IT head | Tool, with Background and webhook | Web, generic webhook, Slack |

Agent types: a Tool agent does a job on request in chat; a Q&A agent answers
from the company's own sources; a Background agent runs unattended on a
schedule or a webhook and leaves its work under `output/`.

## Run one

```bash
cp -r templates/<name> my-hub
hubzoid run my-hub
```

Open <http://localhost:3080>. No `.env` is needed when the `claude` CLI is
installed and logged in (`MODEL` defaults to `claude-local`). To use OpenAI,
Anthropic, Azure OpenAI, or OpenRouter, copy `.env.example` to `.env` and
set `MODEL` and the matching key. See `docs/providers.md`.

Check the hub, fire a scheduled task by hand, and run the evals:

```bash
hubzoid doctor my-hub
hubzoid schedule list my-hub
hubzoid schedule run my-hub <task> --timeout 300 --max-rounds 2
hubzoid eval run my-hub
```

## What every template contains

| Path | What it is |
|---|---|
| `AGENTS.md` | The system prompt with `name`, `description`, `model`, and four `suggestions`. |
| `knowledge/` | One or two markdown files of placeholder company knowledge (thresholds, conventions, escalation rules). Replace with your own. |
| `schedule/` | One or two unattended tasks with a cron or a webhook trigger. See `docs/schedule.md`. |
| `evals/` | One or two behavioural checks. `hubzoid eval run` is the CI gate. See `docs/evals.md`. |
| `tools_local/` | Placeholder tools returning sample data, marked as such in their docstrings. Wire the real system here. |
| `.env.example` | Provider and surface variables. Copy to `.env`. |
| `README.md` | What it does, who uses it, which surfaces to turn on, how to run it. |

## Surfaces

Every template runs on the bundled web UI. Add any combination of the
shipped surfaces in one process:

```bash
hubzoid run my-hub --slack --telegram --whatsapp --webhook
```

Slack needs `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`. WhatsApp and Telegram
use a per-hub `identity/access.csv` roster; unknown senders are rejected
before the agent runs. The generic webhook needs `WEBHOOK_INBOUND_SECRET`.
See `docs/slack.md` and `docs/inbound-surfaces.md`.

Posting an unattended report into a chat channel is done through a
placeholder tool (`post_to_team_chat` in `supplier-slip-check`) that you
point at your chat platform's incoming webhook. The other scheduled
templates leave their report under `output/` as the deliverable.

## Restricting a tool to a group

If a template's data should be reachable only by one group (the ledger by
accounts, the metric store by finance), move the tool file from
`tools_local/` into `restricted/` and grant the Open WebUI group of the same
name. The runtime denies the tool at execution for anyone else and records
allow and deny decisions to the audit log (`hubzoid audit my-hub`). See
`docs/access-management.md`.
