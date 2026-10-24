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

## Run your own report from chat (1.3.1)

`service_snapshot` is a deterministic report: it makes no model calls. Chat
still needs your configured runtime's authentication and can incur model costs.
Use Hubzoid 1.3.1, enable `HUBZOID_SCHEDULES=1`, and start this hub. In the Console,
give your signed-in account **Use agent**, **Run Service snapshot** and, for
repeat runs, **Schedule reports for themselves**. Keep the hub running when
schedules are due. Local sleep does not provide an always-on scheduler.

1. Ask “Run service_snapshot for me.” Open the returned live link. It shows
   the latest synthetic metrics and flags latency above the saved 800 ms rule.
2. Personalize it: replace the synthetic lines in `raw_data/events/` with your
   own metrics, keeping `ts`, `service`, and `p95_ms`. Do not put secrets there.
3. Save a correction explicitly: ask your authoring assistant (Claude Code or
   Codex), “Change knowledge/snapshot-rules.json so p95_ms is 2000. Show me the
   diff.” Or edit the JSON yourself. Chat conversation alone does not save this
   rule or turn it into shared memory.
4. Run the report again. The next report states the new 2000 ms rule; rows
   between 800 and 2000 ms change from Check to OK. The live link stays the same.
5. Ask “Run service_snapshot for me every Monday at 09:00 Asia/Kolkata.” Review
   the proposal and confirm in the next message. Open **My workspace →
   Schedules & reports** to edit its inputs/time, pause, run now, or stop.
6. Invite a teammate and grant only those three capabilities. Their personal
   reports stay separate. Sharing a report is a separate, explicit action.

For a deterministic authoring check without starting the server or using a model:

```python
from pathlib import Path
from hubzoid.workflows.runtime import load_definitions
from hubzoid.workflows.testing import run_local
root = Path(".").resolve()  # run from inside my-watchtower
wf = next(w for w in load_definitions(root) if w.name == "service_snapshot")
result = run_local(wf.fn, hub_dir=root, inputs={})
print(result.result)
```

If the report is absent, check the three grants, `HUBZOID_SCHEDULES`, and restart
after adding workflow code. If it fails, validate the JSON/JSONL and check the
run reference with the builder. Personal email notices are off by default;
results remain in chat and My workspace. No external service is contacted by
this report. Stop its schedule when finished; deleting the hub removes local
operational data, so back it up first if you want to keep it.
