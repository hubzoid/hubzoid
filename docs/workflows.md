# Scheduled work: markdown tasks and code workflows

A hub can do work on its own, on a clock or when a webhook arrives. There are
two ways to write that work, and both run on the same engine.

| | Markdown task | Code workflow |
|---|---|---|
| File | `schedule/<name>.md` | `workflows/<name>/*.py` |
| Write it as | Instructions for the hub's agent, or a `run:` command | Python with `@workflow` and `@step` |
| Best for | Most recurring jobs: write what should happen, the agent does it | Exact steps, loops, thresholds, calls to your own APIs |
| Guide | [schedule.md](schedule.md) | This page |
| Starts on its own | Whenever the file exists | With `HUBZOID_SCHEDULES=1` (on by default in a gateway) |

Start with a markdown task. Move to a code workflow when you need the steps to
be exact. The [Watchtower sample](../hubzoid/templates/watchtower/README.md)
(`hubzoid init my-watchtower --template watchtower`) is a complete code
workflow to copy from.

Both kinds run on the hub's [DBOS](https://docs.dbos.dev) engine, so they share
one run history (`hubzoid schedule status`, the Console's **Runs** page), the
same pause, resume and cancel commands, and the same backup.

## A code workflow

```python
# workflows/daily_report/main.py
from pydantic import BaseModel

from hubzoid import hub, step, workflow


class Summary(BaseModel):
    headline: str
    risks: list[str]


@step(max_attempts=3)
def fetch_numbers() -> dict:
    token = hub.secret("reporting_api_token")   # read secrets inside a step
    ...                                        # call your API
    return {"orders": 1240, "refunds": 31}


@workflow("daily 06:00", timezone="Asia/Kolkata")
def daily_report():
    numbers = fetch_numbers()
    summary = hub.call_llm(f"Summarize these numbers: {numbers}", response_model=Summary)
    return summary.model_dump()
```

- `@workflow(schedule, timezone=None, on_failure=None)`. The schedule is a cron
  expression or plain English: `every 15 minutes`, `every 3 hours`,
  `daily 06:00`, `every monday 08:30`. Leave it out for a workflow you only run
  by hand. `on_failure` can be a URL that gets a POST when a run fails.
- `@step(max_attempts=1)` marks a function whose result is saved. A finished
  step is not run again when the workflow resumes after a restart. Set
  `max_attempts` above 1 only for steps that are safe to repeat.
- `hub.setting(key)` reads `workflows/settings.yaml`. `hub.secret(name)` reads
  the hub's environment. Do not return secrets from a step: administrators can
  see step results.
- `hub.state` is a small durable dictionary per workflow, for things like "last
  item handled". It survives restarts and upgrades.

`hubzoid new workflow <name> <hub>` scaffolds one. `hubzoid doctor <hub>`
checks every workflow file loads.

## Calling a model

| Call | What it does | Retried |
|---|---|---|
| `hub.call_llm(prompt, *, response_format="text", response_model=None, model=None, system=None)` | One model call with no tools. Returns text; with `response_format="json"` a dict (the reply must be a JSON object); with a Pydantic `response_model` a validated instance. | Once, since it has no side effects |
| `hub.call_agent(task, *, response_model=None)` | The hub's full agent, with its tools, skills and knowledge. | Only with `agent_max_attempts: N` in `workflows/settings.yaml`, since a retry can repeat a write |
| `hub.decide(state, questions, *, model="typesafe/jev-1.13")` | Experimental. A typed decision from TypeSafe's Jev through OpenRouter. Each question has a `type` (`noul`, `choice` or `score`), `instructions` and `criteria`; returns the answers with probabilities and confidence. Needs `OPENROUTER_API_KEY`. | Once |

`call_llm` uses the hub's model unless you pass `model`. For LiteLLM models it
uses the provider's JSON mode; on `claude-local` it runs one Claude turn with
no tools. If the reply does not match `response_model`, the call raises
`ModelOutputError`, which keeps the raw reply in `.raw`.

Each call is saved as a step, so a workflow that resumes after a restart does
not pay for the same call twice. Each call also writes a usage row: tokens and
estimated cost appear on the Console's **Overview**.

## Who a workflow acts as

A code workflow acts as `workflow:<name>` and a markdown task as
`workflow:md:<task>`. Grant these identities the permissions their restricted
tools need, in the Console, like a person. Without a grant, restricted tools
are refused and the refusal is in the access log.

## When runs happen

- A code workflow runs one at a time: a slot that comes due while the previous
  run is still going waits for it. Different workflows run side by side. To cap
  how many run at once in a hub, set `max_concurrent_workflows: N` in
  `workflows/settings.yaml`. There is no cap by default.
- Markdown tasks run one at a time per hub, and wait while the hub is answering
  chat.
- After downtime, a markdown task runs once to catch up. A code workflow does
  not backfill: it runs at its next slot.
- Run one now with `hubzoid schedule run <hub> <name>`. It uses the same queue,
  so it never overlaps a scheduled run.

## Restarts, retries and idempotency

A run interrupted by a stop or a crash resumes when the hub starts again.
Finished steps are not repeated. The step that was running when the process
stopped runs again, so a step can run more than once. Make steps that change
something outside the hub safe to repeat:

- Use a key the other system deduplicates on, for example the run's slot or a
  record id, instead of creating a new record each time.
- Check before you write: "does the ticket for this alert already exist?"
- Record what you finished in `hub.state` and skip it next time.

Markdown tasks work the same way for their commit and push steps. Their agent
work is never repeated: an interrupted markdown run is reported as interrupted
and the next slot runs the task.

Runs belong to the workflow code that started them: a hash of the Hubzoid
version and `workflows/**/*.py` (code imported from outside `workflows/` is not
covered). After an edit or an upgrade, runs that were interrupted or queued
under the old code are cancelled at the next start, so they cannot block the
queue. They show as `CANCELLED`. Markdown runs that were queued but not started
are queued again under the new code. Before a planned change:

1. Wait until `hubzoid schedule status` shows no run `PENDING` or `ENQUEUED`,
   ideally between slots.
2. Deploy and restart.
3. Start a fresh run with `hubzoid schedule run` if a cancelled one is still
   needed.

## Pausing, resuming and cancelling

```bash
hubzoid schedule pause <hub> <name>       # stop new runs; a running one finishes
hubzoid schedule resume <hub> <name>
hubzoid schedule cancel <hub> <run-id>
```

These work for both kinds and are recorded in the access log. The Console
shows paused work and the log, but has no run buttons: controls stay with
whoever runs the server. `hubzoid backup` also holds new runs while it copies,
then releases them.

## Watching it

- `hubzoid schedule status <hub>`: definitions, recent runs and errors.
- The Console's **Runs** page: every run across agents, its steps and errors.
- `hubzoid doctor <hub>`: `scheduler.health` reports holds, pauses, and a
  dispatcher that stopped.
