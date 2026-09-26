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
one run history (`hubzoid schedule status`, each agent's **Runs & schedules**
tab in the Console), the same pause, resume and cancel commands, and the same
backup.

## Requirements

Hubzoid supports Python 3.11 and 3.12. **Workflows on Python 3.12:** the workflow engine (DBOS, which runs markdown
schedules and code workflows) needs SQLite 3.42 or newer, or PostgreSQL.
Check with the same Python the hub uses:
`python -c "import sqlite3; print(sqlite3.sqlite_version)"`.
`hubzoid doctor` reports it as `deps.sqlite`. Python 3.11 is not affected.
Without it, the engine refuses to start with a clear message and no scheduled
task or workflow runs. Use a Python build with a newer SQLite (python.org, uv,
Homebrew, Debian 13, Ubuntu 24.04) or PostgreSQL.

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

`hubzoid new workflow <name> <hub>` scaffolds a manual, model-free example.
Run it with `hubzoid schedule run <hub> <function_name>` using the command printed
by the scaffold. Add a schedule only after testing its effects. `hubzoid doctor <hub>`
checks every workflow file loads.

## Calling a model

| Call | What it does | Retried |
|---|---|---|
| `hub.call_llm(prompt, *, response_format="text", response_model=None, model=None, system=None)` | One model call with no tools. Returns text; with `response_format="json"` a dict (the reply must be a JSON object); with a Pydantic `response_model` a validated instance. | Once, since it has no side effects |
| `hub.call_agent(task, *, response_model=None)` | The hub's full agent, with its tools, skills and knowledge. | Only with `agent_max_attempts: N` in `workflows/settings.yaml`, since a retry can repeat a write |
| `hub.call_jev(state, questions, *, model="typesafe/jev-1.13")` | Experimental. Typed decisions from TypeSafe's Jev through OpenRouter: see [Decisions with Jev](#decisions-with-jev). Needs `JEV_OPENROUTER_API_KEY`. | Rate limits, server errors and timeouts: once |

`call_llm` uses the hub's model unless you pass `model`. For LiteLLM models it
uses the provider's JSON mode; on `claude-local` it runs one Claude turn with
no tools. If the reply does not match `response_model`, the call raises
`ModelOutputError`, which keeps the raw reply in `.raw`.

Each call is saved as a step, so a workflow that resumes after a restart does
not pay for the same call twice. Each call also writes a usage row: tokens and
cost appear on the Console's **Agents** dashboard. Cost is the provider's own
figure when it reports one (Jev always does), otherwise an estimate.

## Decisions with Jev

`hub.call_jev` asks [Jev](https://openrouter.ai/docs/guides/community/jev), a
decision model, typed questions about a `state` (text, an object or a list).
It returns answers with probabilities instead of prose. It is experimental, and
OpenRouter's Decisions API is in alpha, so its shapes may change.

| Type | Asks | `criteria` | Answer |
|---|---|---|---|
| `noul` | Does this hold? | Optional: `{"true": ..., "false": ...}` | `{"type": "noul", "noul": 0.97}` (the probability of yes) |
| `choice` | Which one? | Two or more `{label: guidance}` | `{"type": "choice", "choice": "billing", "probabilities": {label: p}, "confidence": 0.9}` |
| `score` | Where on this scale? | A list of two or more levels, lowest first | `{"type": "score", "score": 1.8, "probabilities": {"0": p, ...}, "legend": {...}, "confidence": 0.8}` |

```python
# workflows/triage/main.py
from hubzoid import hub, step, workflow

URGENCY = ["Low: can wait a week", "Medium: handle within a day", "High: business blocked"]


@step
def page_on_call(ticket: str) -> None:
    ...


@workflow()
def triage():
    ticket = "Nobody can log in and orders are blocked."
    answers = hub.call_jev(ticket, {
        "is_billing": {"type": "noul", "instructions": "Is this a billing problem?"},
        "team": {"type": "choice", "instructions": "Which team should handle it?",
                 "criteria": {"billing": "Charges and refunds",
                              "technical_support": "Errors and outages",
                              "account_support": "Logins and settings"}},
        "urgency": {"type": "score", "instructions": "How urgent is it?", "criteria": URGENCY},
    })
    if answers["urgency"]["score"] >= 1.5:
        page_on_call(ticket)
    return answers["team"]["choice"]
```

One request can mix the three types, and answers come back by question name.
Every answer is checked before it is returned: a `choice` is one of your labels,
a `noul` is between 0 and 1, and a `score` is within the scale. A missing,
empty or malformed answer raises an error. It never comes back as an empty
result. Treat probabilities and confidence as signals, not proof that an
answer is right.

A question has only `type`, `instructions` and `criteria`. Instructions and
each criteria entry are non-empty text, an object or a list. Anything else is
refused before a request is made.

Set `JEV_OPENROUTER_API_KEY` in the hub's `.env` to a dedicated OpenRouter key.
Jev never falls back to `OPENROUTER_API_KEY`, the hub's chat model, or any
other credentials, and the chat model never uses this key.

### Usage

Each `call_jev` writes one usage row, including any retry:

| Field | Value |
|---|---|
| `kind` | `jev` |
| `subject`, `surface` | For a workflow, the account the run acts as on `workflow` (`workflow:<name>` only for a legacy hub's service identity); for chat, the person and their channel (`web`, `api`) |
| `chat_id` | The chat, for chat calls |
| `model` | The version that answered, such as `typesafe/jev-1.13-20260917`; the requested model if the call failed |
| `input_tokens`, `output_tokens`, `cost_usd` | As OpenRouter reports them. Jev bills input tokens only |
| `status` | `ok`, or `error` for a call that failed, including one refused after a malformed reply |

A row names the workflow, not the run. To see a run's calls, open the run: each
`call_jev` is a step there with the OpenRouter request id in its result.

### Failures and retries

Failures raise `hubzoid.jev.JevError`, and the step and the run fail with its
message. A missing key, a rejected key (401 or 403), missing credits (402) or
invalid questions fail at once. Rate limits (429), server errors and timeouts
are retried once, after the `Retry-After` delay OpenRouter sends (seconds or a
date) or one second without one. If OpenRouter asks for more than 10 seconds,
the call fails at once with that delay in its message instead of waiting. A
finished call is not made again when a run resumes. A call that was still in
flight when the process stopped has no saved answer, so the resumed run asks
again and may be billed twice. Jev calls are at least once, not exactly once.

### In chat

The same adapter is also a chat tool, `call_jev`, disabled until an
administrator grants the **Call Jev** capability (`jev`) in the
Console. It is not available on Slack, WhatsApp, Telegram or the hosted MCP
server. See [access management](access-management.md#decisions-with-jev-in-chat).

## Who a workflow acts as

Every run acts as an ordinary account: `@workflow(run_as="priya@company.com")`
(or `run_as:` in a markdown task), else `HUBZOID_WORKFLOW_USER`, else the owner
recorded at setup. That account's grants decide what the run may do, and its
`hub.state`, artifacts, email and connections are that person's. A missing or
unusable account fails the run with the fix; there is no fallback. See
[workflow-identity.md](workflow-identity.md).

To hand results to that person, publish a file as an artifact and email a link:
`hub.publish_artifact(path, title=...)` and `hub.send_email(subject, body,
artifacts=[...])`. See [reports-and-email.md](reports-and-email.md).

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
work is not re-run inside the interrupted run: it is reported as interrupted,
and the next slot runs the task again from the start. Anything the interrupted
run already did outside the hub is not undone, so write tasks that check before
they act.

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
- The Console's **Runs & schedules** tab on each agent: its runs, steps and errors.
  A hub's managers see each run's workflow, status, timing and a failure
  summary. What a run produced (its result, step outputs and data-bearing
  errors) is shown only to the account the run acted as. The exception is a
  legacy service run, which acts for no person. `hubzoid schedule status` on
  the server shows everything.
- `hubzoid doctor <hub>`: `scheduler.health` reports holds, pauses, and a
  dispatcher that stopped.
