# it-ops-digest

The on-call engineer coming on shift gets one short digest: what fired
overnight, what was suppressed and by which rule, what is still open at
hand-over, and the first two runbook steps for each open incident. In chat,
anyone on the IT-ops team can ask "what is the runbook for DiskFull" and get
the steps from the team's own runbook file, quoted, not improvised.

## Who on the team uses it

The on-call engineer at hand-over, the IT-ops lead, and the IT head.

## What it does

| Mode | What happens |
|---|---|
| Scheduled | `schedule/morning-digest.md` runs at 07:03 daily and writes `output/digests/<date>.md`. |
| Webhook | `schedule/alert-intake.md` fires when an alert event lands on the generic webhook and appends an intake line, with the first runbook step, to `output/alerts/`. |
| Chat | Answers "what fired", "what is open", "show me the runbook for X". |

Agent type: Tool (runbook lookup) plus Background (scheduled and webhook).

## The runbook tool pattern

`tools_local/alerts.py` has a real tool, `runbook(alert_name)`, that reads
`knowledge/runbooks.md` and returns the section under the matching
`## AlertName` heading. The runbooks stay in a markdown file the team
already edits. Add a section and the agent can reach it by name. The agent
is told to quote steps, never to add its own, and the `runbook-lookup` eval
checks that.

## Run it

```bash
cp -r templates/it-ops-digest my-hub
hubzoid run my-hub
```

Open <http://localhost:3080> and ask what fired overnight. No `.env` is
needed when the `claude` CLI is installed and logged in; copy `.env.example`
to `.env` to pick another provider.

Test the tasks and the evals:

```bash
hubzoid doctor my-hub
hubzoid schedule run my-hub morning-digest --timeout 300 --max-rounds 2
hubzoid eval run my-hub
```

## Surfaces to turn on

The generic webhook receives alert events from your alerting system. Set in
`.env`:

```dotenv
WEBHOOK_INBOUND_SECRET=<a shared secret>
WEBHOOK_INBOUND_NAME=alerting
```

and run with the webhook surface on. The endpoint is
`/webhooks/<hub>/alerting`, and `schedule/alert-intake.md` handles each
event:

```bash
hubzoid run my-hub --webhook
hubzoid run my-hub --webhook --slack    # plus Slack for the on-call channel
```

See `docs/inbound-surfaces.md` for authentication options (shared secret or
HMAC) and `docs/slack.md` for Slack.

## Where the real systems plug in

`alerts_since` and `open_incidents` in `tools_local/alerts.py` are
placeholders with sample data. Your team replaces the body of each with a
call into the alerting system and the incident tracker and keeps the
signature and return shape. Delivery of the digest into the on-call channel
is also a placeholder; the file under `output/digests/` is the deliverable
until a posting tool is added.

## Make it yours

1. Replace the sections in `knowledge/runbooks.md` with your own runbooks,
   one `## AlertName` per alert.
2. Set your hand-over window, suppression rules, and stale threshold in
   `knowledge/handover-format.md`.
3. Point the evals at your own alert names.
