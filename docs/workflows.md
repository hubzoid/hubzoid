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
  by hand. `on_failure` is a URL, or the name of an environment variable that
  holds one, that gets an alert when a run fails (see
  [Deadlines and alerts](#deadlines-and-alerts) for what changed in 1.1).
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
shows paused work and the log, but has no run buttons. On the server, controls
act with the authority of whoever runs the command. People you grant it can
also use them from chat or an assistant (next section). `hubzoid backup` also
holds new runs while it copies, then releases them.

## From chat and assistants

An agent can list its workflows, report on runs, start a run, and pause,
resume or cancel, for the people you allow. Nobody has these tools until a
manager grants them in the Console: **Agents → the agent → Access**, then
under **Hubzoid tools → Workflows**:

| Capability | Tools | What it allows |
|---|---|---|
| See workflows and runs (`workflows_view`) | `list_workflows`, `workflow_runs` | Workflows and schedules in this agent, their state, next and last run, and recent runs with their steps. |
| Run and control workflows (`workflows_manage`, sensitive) | `run_workflow`, `pause_workflow`, `resume_workflow`, `cancel_workflow_run` | Start a run now, pause or resume a schedule, cancel a queued or running run. |

- The tools act only on the agent they run in, for both code workflows and
  Markdown tasks.
- A run started from chat acts as the workflow's own account (its `run_as`),
  never as the person asking. That person is recorded as the one who started
  it (`run_start` in the access log, with the surface).
- Results stay private as before: output and error details are shown only to
  the account the run acted as. Others see the status and the error type.
- `run_workflow` returns the existing run when one is already queued or
  running, and refuses while a backup holds new runs. Code workflows must be
  running on the agent (`HUBZOID_SCHEDULES=1`, or under `hubzoid gateway`).
- The agent names the workflow back and waits for a yes before it acts. That
  is guidance for the model. The grant, the per-call check and the audit are
  the controls.
- The tools work in web chat, the API, MCP, WhatsApp and Telegram. They are
  never offered on Slack or inside a scheduled run, so a workflow cannot
  start or pause another one.
- `HUBZOID_WORKFLOW_TOOLS=false` in a hub's `.env` removes them from that
  agent. The Console then shows the capabilities as disabled.
- On an agent whose access is still managed in the chat app (not yet migrated
  to the Console), these capabilities are granted the legacy way, like
  restricted tools: membership in an Open WebUI group named `workflows_view`
  or `workflows_manage`. Nobody has them until an administrator creates that
  group. The access tools work only on Console-managed agents.

Workflows can't be created or edited from chat. Write them in `workflows/` or
`schedule/` as described above.

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

## Named webhook workflows (1.1)

The bridge receives named webhooks even when scheduled code workflows are off.
Add a declaration to `workflows/settings.yaml`:

```yaml
max_executor_threads: 32
webhooks:
  ticket-updated:
    verify: header
    header: X-Webhook-Secret
    event_key: "{ticket.id}:{ticket.modifiedTime}"
webhook_retry_delays: [60, 300]
alerts:
  to:
    - webhook: ALERT_WEBHOOK_URL
  cooldown: 1h
  failures_in_a_row: 3
  pause_after_failures: 20
```

Set `WEBHOOK_SECRET_TICKET_UPDATED` in the hub environment. A sender POSTs a JSON
object to `/webhooks/<hub-slug>/ticket-updated`, with that secret in the configured
header. Bodies are limited to 256 KiB. Query parameters are not credentials.
For timestamped HMAC use `verify: hmac`, `signature_header: X-Signature`, and
`timestamp_header: X-Timestamp`. The signature is `sha256=` plus the hex HMAC-SHA256
of `<timestamp>.<raw body>` using the secret. Timestamps are Unix seconds and
must be within five minutes. Provider-specific signing formats need their own
verified adapter. This is not a claim of native authentication for every provider.

Webhook names are lowercase letters, digits, `-` and `_`. `whatsapp`, `telegram`
and the hub's legacy inbound webhook name (`WEBHOOK_INBOUND_NAME`, default
`webhook`) are already routes of the hub's inbound server, so they are refused
rather than silently taken over.

A mistake in `workflows/settings.yaml` (bad YAML, an unknown verification, an
empty alert destination) turns the workflow engine off for that hub and records
the error in workflow health, which the edge reports and alerts on. Chat, and
every other hub of a gateway, keep running. Its webhook URLs answer 503 until
the file is fixed and the bridge restarts.

```python
from hubzoid import workflow, step, hub

@workflow(on_webhook="ticket-updated", concurrency=2,
          concurrency_key="ticket.id", timeout="15m")
def ticket_updated():
    event = hub.event
    decision = hub.call_jev(
        state=event.body,
        questions={"needs_review": {
            "type": "noul", "instructions": "Does this ticket need human review?"
        }},
        timeout=90,
    )
    # A destination write should use event.key for idempotency, or check the
    # current ticket state before applying the same change again.
    return decision
```

`hub.event` has `id`, `key`, `body`, safe `headers`, `webhook`, `received_at`, and
`attempt`. It is `None` for manual and scheduled calls, and authors accepting
those triggers must handle that case. One webhook has one consumer. Workflow
queues apply the same concurrency policy to all triggers. A missing partition
field uses a shared `missing` partition. `concurrency=2` permits two runs overall
while the same ticket remains serialized. The bounded thread pool is hub-wide.
A backlog on one webhook workflow does not hold up events for another.

A 200 means the event is durably stored, not that the handler succeeded. Repeats
with the same hub, webhook, key and body attach to it, including after completion.
Changed content under that key returns 409 and records an alert. Without an
`event_key`, provider delivery-id headers are used, then a warned body-hash
fallback. Retries belong to Hubzoid: by default three attempts, after 60 and 300
seconds. External actions remain at-least-once across a crash. A checkpoint
cannot undo or guarantee deduplication of a remote write.

Inspect deliveries with `hubzoid schedule deliveries <hub>`. Failed events can
be retried explicitly with `hubzoid schedule redrive <event-id> --hub <hub>` after
checking side effects. Cancelling a webhook run from the Console fails its event
with that advice instead of retrying it. Event identity and digest records have
no automatic retention expiry, so old sender retries remain deduplicated.
Include the operational store in backups.

After a code change, an event that never started runs on the current code, found
by its webhook name, so renaming the workflow function is safe. An event whose
attempt had started under the old code is failed visibly for an explicit
redrive. It is never replayed silently on changed code.

### One engine per hub

Only one engine owns a hub. A lifetime SQLite file lock or PostgreSQL advisory
lock enforces exclusion. Expiring readiness closes admission but never grants a
second engine permission to run. `hubzoid schedule run` hands work to the live
owner. With no live owner it owns the hub for that one run and serves only that
workflow's queue: interrupted unrelated runs are queued again for the bridge,
not run by the command. A bridge that starts while another process owns the hub
keeps chat running and retries every 15 seconds.

If a PostgreSQL owner loses its database session, it stops claiming queued work
at once, keeps chat running, and retries ownership every 15 seconds. A run it had
already claimed stops at its next guarded point without recording a failure, so
the next owner recovers it, as after a crash. Guarded points are `@step`
functions, model, agent and Jev calls, emails, artifacts, and a markdown task's
start, commit and push. Code that ran before that point may run again
(at-least-once). A model or agent call already in progress, with its tools,
is not interrupted. On PostgreSQL a new owner also
waits for the previous owner's lease to lapse, so after a crash workflows
resume up to 90 seconds later. A clean stop releases the lease at once.

## Deadlines and alerts

`hub.call_llm(..., timeout=120)`, `hub.call_jev(..., timeout=90)` and
`hub.call_agent(..., timeout=600)` accept seconds or strings such as `"2m"`.
The smallest enclosing run or call deadline wins, including retries. Webhook runs
have a default 15-minute deadline. Scheduled runs have no default run deadline.
`workflow_timeout` in settings supplies a hub default, and `@workflow(timeout=...)`
overrides it. Arbitrary synchronous Python cannot be safely killed: an overdue
run alerts but keeps its thread and ticket partition until it exits. A run that
returns successfully just after its deadline stays successful, with a timeout
alert, so its side effects are not repeated. Authors must bound their own
network and file calls. The remote server may still process a request after the
client closes, so use destination idempotency for writes.

Hubzoid's `call_jev(state, questions, model=...)` interface is stable. Its provider
adapter remains the versioned OpenRouter **alpha** Decisions API, currently
`typesafe/jev-1.13`, with a dedicated `JEV_OPENROUTER_API_KEY`. Provider schema or
model changes are not covered by that interface promise.

Alerts cover failed runs (webhooks only after attempts exhaust), failed scheduled
evals, overdue runs, failure streaks, schedules that stop dispatching and engine
health. Run failures and timeouts have a default one-hour cooldown, and
suppressed occurrences are counted in the next alert. Twenty consecutive failed
**scheduled** runs pause that schedule through the existing controls. Manual,
Console and webhook runs neither count toward that nor reset it. Set
`pause_after_failures: 0` to disable auto-pause. Webhook events are not paused.
Resume from the Console or `hubzoid schedule resume` after fixing the cause.

The engine checks for finished runs every 30 seconds. It reads only runs that
finished since its last check, by completion time, from a cursor kept in the
operational store, so a long outage is caught up and history size does not
slow it down. On its first start it looks back 24 hours.

Destinations under `alerts.to` accept `webhook: ENV_VAR`, `slack: ENV_VAR` (incoming
webhook), and `email: address`. Email uses the existing deployment SMTP settings.
Preview mode writes private `.hubzoid/alert-preview/` files. `@workflow(alert_to=...)`
or a markdown task's `alert_to:` overrides the hub destinations. `HUBZOID_ALERT_URL`
is the deployment fallback. Missing destinations and exhausted deliveries are
visible in `schedule deliveries`. They do not disappear as successful sends.

`on_failure` from 1.0.x still works, with three changes:

- It goes through this alert outbox, so the POST body is the alert payload
  below. It has no `error` field, and exception text never leaves the server.
- The one-hour cooldown applies: repeated failures within the hour arrive as
  one alert with a count.
- A value that is not an `http://` or `https://` URL is read as the name of an
  environment variable holding the URL. In 1.0.x it was only logged.

Delivery is durable and retried up to five times, at-least-once. Webhook messages
carry `Idempotency-Key`, `X-Hubzoid-Timestamp` and, when `HUBZOID_ALERT_SECRET` is
set, `X-Hubzoid-Signature` (HMAC-SHA256 of timestamp + `.` + raw body). The body
is JSON with `hub`, `kind`, `url` (a Console link) and, when they apply,
`workflow`, `run_id` and `count`. Private outputs and raw exception text stay
out. Slack and email receivers may still see a duplicate after an ambiguous
transport failure.

The edge independently watches engine readiness and sends stale and recovered
alerts. `GET /healthz/workflows` returns 503 if a configured hub is unhealthy,
without exposing hub names or errors. Monitor this URL **externally** and
supervise the process: an edge cannot report its own host's death. Bridge
liveness remains separate from workflow health. This endpoint requires the
edge. A standalone headless bridge should be monitored by its supervisor.
