# accounts-desk

The first pass on every supplier bill. The agent reads the bill, finds its
purchase order, checks amount and quantity against tolerance, looks for
duplicates, and checks the supplier's standing. Good bills get a draft entry
in your chart of accounts. Everything else becomes an exception with the
rule it broke and the person who decides. The finance team reviews
exceptions only.

## Who on the team uses it

The accounts team, day to day. The finance head, for the exceptions that
reach them. Nothing is posted by the agent; every entry is a draft.

## What it does

| Mode | What happens |
|---|---|
| Scheduled | `schedule/nightly-bill-check.md` runs six evenings a week and writes `output/accounts/exceptions-<date>.md`. |
| Chat | Checks one bill on request, explains a verdict, drafts an entry. |

Agent type: Tool (chat) plus Background (scheduled).

## Run it

```bash
cp -r templates/accounts-desk my-hub
hubzoid run my-hub
```

Open <http://localhost:3080> and try "Check bill INV-2291 from Arcadia
Packaging against its PO". No `.env` is needed when the `claude` CLI is
installed and logged in; copy `.env.example` to `.env` to pick another
provider.

Test the nightly task and the evals:

```bash
hubzoid doctor my-hub
hubzoid schedule run my-hub nightly-bill-check --timeout 300 --max-rounds 2
hubzoid eval run my-hub
```

## Surfaces to turn on

The web UI is bundled and is the natural desk for this hub, since bills are
pasted or uploaded. Slack works well for quick "is this a duplicate"
questions:

```bash
hubzoid run my-hub --slack      # SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env
```

If the ledger tools should be reachable only by the accounts group, move
`tools_local/ledger.py` into `restricted/` and grant the group of the same
name. See `docs/access-management.md`.

## Where the real systems plug in

`tools_local/ledger.py` holds four placeholder tools with sample data:
`find_purchase_order`, `search_bills`, `pending_bills`, `supplier_profile`.
Your team replaces the body of each with a call into the accounting system
or ERP and keeps the signature and return shape. Nothing else changes.

## Make it yours

1. Replace `knowledge/chart-of-accounts-conventions.md` with your codes and
   entry shape.
2. Set your tolerances and escalation table in `knowledge/matching-rules.md`.
3. Point the evals at bills from your own ledger.
