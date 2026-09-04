# supplier-slip-check

Every night, the agent compares the delivery slips suppliers handed over at
the gate with what the warehouse recorded in the goods receipt ledger.
Mismatches are posted to the operations channel before the morning shift,
one line each, with the difference and the owner. A person corrects; the
agent never touches the ledger.

## Who on the team uses it

Warehouse team leads and the operations head. In chat, anyone on the
operations team can ask why a slip was flagged or what a tolerance is.

## What it does

| Mode | What happens |
|---|---|
| Scheduled | `schedule/overnight-reconciliation.md` runs at 02:17 daily, writes `output/reconciliation/<date>.md`, and posts the mismatch list to the team chat. Catches up missed nights from its state file. |
| Chat | Reconciles a date on request and explains the rules. Posts to chat only when asked. |

Agent type: Background (scheduled) with a Q&A surface.

## Run it

```bash
cp -r templates/supplier-slip-check my-hub
hubzoid run my-hub
```

Open <http://localhost:3080> and try "Reconcile the supplier slips for
2026-09-01" (the date the sample data covers). No `.env` is needed when the
`claude` CLI is installed and logged in; copy `.env.example` to `.env` to
pick another provider.

Test the nightly task and the evals:

```bash
hubzoid doctor my-hub
hubzoid schedule run my-hub overnight-reconciliation --timeout 300 --max-rounds 2
hubzoid eval run my-hub
```

## Surfaces to turn on

The scheduled post goes out through the `post_to_team_chat` tool, which you
point at your chat platform's incoming webhook. For questions from the
warehouse floor, the Slack or WhatsApp surface fits:

```bash
hubzoid run my-hub --slack       # SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env
hubzoid run my-hub --whatsapp    # WHATSAPP_* in .env, plus identity/access.csv
```

WhatsApp and Telegram use a per-hub `identity/access.csv` roster; unknown
senders are rejected before the agent runs. See `docs/inbound-surfaces.md`.

## Where the real systems plug in

`tools_local/slips.py` holds three placeholders: `supplier_slips` (gate
system export), `ledger_receipts` (goods receipt table in your ERP), and
`post_to_team_chat` (your chat webhook; the placeholder records the message
and sends nothing). Your team replaces the body of each and keeps the
signature and return shape.

## Make it yours

1. Set your tolerances per material class in
   `knowledge/reconciliation-rules.md`.
2. Put your supplier owners and post wording in
   `knowledge/escalation-rules.md`.
3. Point the evals at a real date from your own data.
