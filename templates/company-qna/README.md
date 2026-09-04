# company-qna

Plain-language questions about the company's own numbers and policies,
answered for the team from the company's own sources. "What did we bill
last month, and to whom." "How long do I have to file an expense claim."
"Who approves a purchase above 200,000." Every answer names its source and
period, and a question the sources cannot answer gets "not available" rather
than a guess.

## Who on the team uses it

Everyone. Team leads checking a number before a meeting, new joiners asking
about leave, warehouse leads asking about procurement limits. It is the
front door to the numbers and the handbook without a login to the reporting
system.

## What it does

| Mode | What happens |
|---|---|
| Chat | Answers from the metric store (`tools_local/company_numbers.py`) and the policy file (`knowledge/policies.md`). |
| Scheduled | `schedule/weekly-numbers-note.md` writes a six-line numbers note every Monday to `output/notes/`. |

Agent type: Q&A.

## Run it

```bash
cp -r templates/company-qna my-hub
hubzoid run my-hub
```

Open <http://localhost:3080> and try "What did we bill last month, and to
whom". No `.env` is needed when the `claude` CLI is installed and logged in;
copy `.env.example` to `.env` to pick another provider.

Run the evals and test the weekly note:

```bash
hubzoid doctor my-hub
hubzoid eval run my-hub
hubzoid schedule run my-hub weekly-numbers-note --timeout 300 --max-rounds 1
```

## Surfaces to turn on

This hub earns its keep where the team already is:

```bash
hubzoid run my-hub --slack --telegram
```

Slack needs `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`. Telegram needs
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and an
`identity/access.csv` roster so that only known senders reach the agent.
See `docs/slack.md` and `docs/inbound-surfaces.md`. For the web UI with
login, set `WEBUI_AUTH=true` and pick SSO per `docs/auth.md`.

If some metrics should be visible only to the finance group, move
`tools_local/company_numbers.py` into `restricted/` and grant the group.
See `docs/access-management.md`.

## Where the real systems plug in

`tools_local/company_numbers.py` holds two placeholders: `list_metrics` and
`metric`. Your team replaces the body of `metric` with a query against the
reporting database or BI layer and keeps the signature and return shape.
Keep `knowledge/metric-definitions.md` in step with the names the store
returns.

## Make it yours

1. Replace `knowledge/policies.md` with your handbook text, keeping short
   section headings.
2. Replace `knowledge/metric-definitions.md` with your metrics and their
   definitions.
3. Point the evals at your own policy facts and metric names.
