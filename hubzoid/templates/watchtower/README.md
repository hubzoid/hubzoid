# Watchtower

A sample hub for automation: a scheduled workflow checks service metrics
against thresholds and, when a service crosses one, writes a report that
explains it. People can then ask the Watchtower agent about it in chat.

The metrics are bundled synthetic data (`raw_data/`). The model call and the
workflow engine are real, so a run needs model credentials, the same as chat.

## Run it once

```bash
hubzoid init my-watchtower --template watchtower
cd my-watchtower
hubzoid schedule run . watchtower
cat output/watchtower/latest.md
```

The report names the failing service, the peak numbers, the likely cause and
what to check first. Detection is plain Python in
`workflows/watchtower/main.py`; only the explanation comes from the model, as
a validated structure (`Finding`).

Run it again: nothing new is reported, because the breach is already
explained. The workflow remembers that in `hub.state`, which survives
restarts.

## Let it run on schedule

Code workflows run on schedule only where you turn them on, so a copy of the
hub on a laptop does not fire alongside the server:

```bash
echo "HUBZOID_SCHEDULES=1" >> .env
hubzoid run .
```

The workflow then runs every 15 minutes while the hub is up. Open the Console at
`/portal/` to see each run under **Runs**, and the usage on **Overview**. Ask
the agent in chat: "What did the last Watchtower check find?"

## See a failure, then recover

```bash
cp raw_data/samples/broken.jsonl raw_data/events/
hubzoid schedule run . watchtower     # exits 1: broken.jsonl line 2 is not valid JSON
hubzoid schedule status .             # the failed run and its error
rm raw_data/events/broken.jsonl
hubzoid schedule run . watchtower     # succeeds again
```

A failed run shows in the Console's Runs page with the same message.

## Make it yours

- Thresholds: `workflows/settings.yaml`. The next run reads them.
- Data: write lines of the same shape into `raw_data/events/`, from a
  scheduled export or a webhook task (see the schedule docs for
  `on_webhook:`).
- Pause and resume: `hubzoid schedule pause . watchtower`, then `resume`.

## Clean up

Delete the folder. Hubzoid keeps this hub's state inside it.
