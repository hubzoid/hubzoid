# stock-drift-watch

Watches stock across every location for two things the team usually finds
too late: drift between what the system says and what was last counted, and
items that have stopped moving. Both are flagged with the money at stake and
the action from your own policy, early enough that a recount or a transfer
is still cheap.

## Who on the team uses it

The inventory controller, the warehouse lead at each location, and the
operations head. The finance head sees the Decide band of slow-movers once a
month.

## What it does

| Mode | What happens |
|---|---|
| Scheduled, weekly | `schedule/weekly-drift-scan.md` runs Mondays at 05:37 and writes `output/stock/drift-<date>.md`. |
| Scheduled, monthly | `schedule/monthly-slow-movers.md` runs on the first of the month and writes `output/stock/slow-movers-<month>.md`. |
| Chat | Answers "where is stock drifting", "what is slow in Coimbatore", "show movements for a SKU". |

Agent type: Background (scheduled) with a Q&A surface.

## Run it

```bash
cp -r templates/stock-drift-watch my-hub
hubzoid run my-hub
```

Open <http://localhost:3080> and ask where stock is drifting. No `.env` is
needed when the `claude` CLI is installed and logged in; copy `.env.example`
to `.env` to pick another provider.

Test the scheduled tasks and the evals:

```bash
hubzoid doctor my-hub
hubzoid schedule run my-hub weekly-drift-scan --timeout 300 --max-rounds 2
hubzoid schedule run my-hub monthly-slow-movers --timeout 300 --max-rounds 2
hubzoid eval run my-hub
```

## Surfaces to turn on

The web UI is bundled. Warehouse leads on the floor are usually on WhatsApp
or Slack:

```bash
hubzoid run my-hub --slack       # SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env
hubzoid run my-hub --whatsapp    # WHATSAPP_* in .env, plus identity/access.csv
```

See `docs/slack.md` and `docs/inbound-surfaces.md` in the Hubzoid repo.

## Where the real systems plug in

`tools_local/inventory.py` holds three placeholders with sample data:
`locations`, `stock_snapshot`, `sku_movement`. Your team replaces the body
of each with a call into the warehouse management system or ERP and keeps
the signature and return shape. Keep drift precomputed in the tool so the
agent reads a number rather than doing arithmetic on raw rows.

## Make it yours

1. Set your drift rows, item classes, and stale-count window in
   `knowledge/drift-thresholds.md`.
2. Set your bands, exclusions, and deciders in
   `knowledge/slow-mover-policy.md`.
3. Point the evals at SKUs from your own data.
