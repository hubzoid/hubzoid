# morning-briefing

One page for the leadership team, every working morning, across sales, cash,
and operations. Written before the day starts from the company's own systems,
with the one thing that needs a decision today at the bottom.

## Who on the team uses it

The managing director, the finance head, and the operations head. In chat,
any of them can ask follow-up questions ("which customers are overdue") and
get an answer from the same tools with the same thresholds.

## What it does

| Mode | What happens |
|---|---|
| Scheduled | `schedule/daily-briefing.md` runs weekdays at 06:23 and writes `output/briefings/<date>.md`. |
| Chat | Answers questions across the three systems using the thresholds in `knowledge/briefing-format.md`. |

Agent type: Background (scheduled) with a Q&A surface.

## Run it

```bash
cp -r templates/morning-briefing my-hub
hubzoid run my-hub
```

Open <http://localhost:3080> and ask for this morning's briefing. No `.env`
is needed when the `claude` CLI is installed and logged in; copy
`.env.example` to `.env` to pick another provider.

Test the scheduled task without waiting for the cron:

```bash
hubzoid doctor my-hub
hubzoid schedule run my-hub daily-briefing --timeout 300 --max-rounds 2
cat my-hub/output/briefings/*.md
```

Run the evals:

```bash
hubzoid eval run my-hub
```

## Surfaces to turn on

The web UI is bundled. For the leadership team on their phones, add Slack or
Telegram:

```bash
hubzoid run my-hub --slack      # SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env
hubzoid run my-hub --telegram   # TELEGRAM_* in .env, plus identity/access.csv
```

See `docs/slack.md` and `docs/inbound-surfaces.md` in the Hubzoid repo.

## Where the real systems plug in

`tools_local/company_systems.py` holds three placeholder tools that return
sample data: `sales_snapshot`, `cash_position`, `ops_snapshot`. Your team
replaces the body of each with a call into the real system (ERP, accounting
package, bank feed, warehouse system) and keeps the signature and return
shape. Nothing else in the hub changes.

Delivery of the scheduled briefing into a chat channel is also a placeholder:
the file under `output/briefings/` is the deliverable until a posting tool is
added to `tools_local/`.

## Make it yours

1. Replace `knowledge/company-context.md` with your company's plan numbers,
   large customers, and owners.
2. Adjust the thresholds in `knowledge/briefing-format.md`.
3. Change the cron in `schedule/daily-briefing.md` to your morning.
4. Edit the evals so they assert your own facts.
