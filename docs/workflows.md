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
be exact. `hubzoid new workflow <name> <hub>` scaffolds one, and the
[Engineering Desk hub](https://github.com/hubzoid/engineering-desk-hub) is a complete set of code
workflows to read.

Both kinds run on the hub's [DBOS](https://docs.dbos.dev) engine, so they share
one run history (`hubzoid schedule status`, each agent's **Runs & schedules**
tab in Settings), the same pause, resume and cancel commands, and the same
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
`ModelOutputError`, which keeps the raw reply in `.raw`. A call that fails
after its retry raises its last error, so `except ModelOutputError` works
inside the engine too.

Each call is saved as a step, so a workflow that resumes after a restart does
not pay for the same call twice. Each call also writes a usage row: tokens and
cost appear in the **Agents** section of Settings. Cost is the provider's own
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
| `subject`, `surface` | For a workflow, the account the run acts as on `workflow` (`workflow:<name>` only for a run recorded before workflows ran as people); for chat, the person and their channel (`web`, `api`) |
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
administrator grants the **Call Jev** capability (`jev`) in
Settings. It is not available on Slack, WhatsApp, Telegram or the hosted MCP
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

These work for both kinds and are recorded in the access log. Settings
shows paused work and the log, but has no run buttons. On the server, controls
act with the authority of whoever runs the command. People you grant it can
also use them from chat or an assistant (next section). `hubzoid backup` also
holds new runs while it copies, then releases them.

## From chat and assistants

An agent can list its workflows, report on runs, start a run, and pause,
resume or cancel, for the people you allow. Nobody has these tools until a
manager grants them in Settings: **Agents → the agent → Access**, then
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
  agent. Settings then shows the capabilities as disabled.

Workflows can't be created or edited from chat. Write them in `workflows/` or
`schedule/` as described above. To let people run a report for themselves, see
[Reports people run and schedule in chat](#reports-people-run-and-schedule-in-chat-13).

## Reports people run and schedule in chat (1.3)

A code workflow can be a report people run from chat, for themselves, and keep
fresh on their own schedule. Everything here is optional; a workflow without
these options works as before.

```python
# workflows/program_report/main.py
from typing import Literal

from pydantic import BaseModel, Field

from hubzoid import hub, step, workflow


class ProgramReport(BaseModel):
    program: Literal["Intro Spandana", "Inner Engineering"] = Field(description="The program.")
    period: Literal["week", "month"] = Field("week", description="The week or the month so far.")


@step
def build_page(program: str, period: str) -> dict:
    ...                                   # write hub.run_dir / "report.html"
    return {"path": str(path), "digest": digest_of_the_numbers}


@workflow("daily 06:00", timezone="Asia/Kolkata", run_as="reports@company.org",
          inputs=ProgramReport, schedule_input={"program": "Inner Engineering"},
          as_tool=True, schedulable=True)
def program_report():
    """Program registrations for one program, for the week or the month."""
    page = build_page(hub.input.program, hub.input.period)
    key = f"program-{hub.input.program.lower().replace(' ', '-')}-{hub.input.period}"
    return hub.publish_artifact(page["path"], title=f"{hub.input.program} report",
                                key=key, digest=page["digest"], audience="domain")
```

| Option | What it does |
|---|---|
| `inputs=Model` | A Pydantic model. The run reads it as `hub.input`. Chat and MCP tools take its JSON schema, Run now in Settings builds a form from it, and the CLI takes `--input key=value`. Inputs are validated on every run. Keep them short and never secret: they are stored with the run. `Field(json_schema_extra={"hidden": True})` shows a value as •••• in lists. |
| `max_input_kb=200` | Raises the inputs limit (4 KB) for a large value such as a diff, up to 256 KB. Not with `schedulable=True`. |
| `schedule_input={...}` | The inputs for the declared schedule. Required when the model has required fields and the workflow has a schedule. |
| `as_tool=True` | A chat and MCP tool named after the workflow ("Run for me"). It runs as the person asking, with their grants, and waits up to `tool_wait` (default 60 s, at most 10 minutes) before handing back a run id ("is it ready?" finds it). Return `{"answer": "..."}` to show text in chat after any links (up to 8,000 characters, marked as data); return `{"refused": "..."}` to show a refusal as is. `confirm=True` asks before running. `surfaces=("web", "mcp")` limits where the tool appears. `sensitive=True` marks the grant Sensitive in Settings. |
| `schedulable=True` | People can save their own schedule of it from chat (needs `as_tool`). `min_interval="6h"` raises the shortest cadence for this workflow. |
| `catch_up=True` | The declared schedule runs once after downtime, unless the next slot is close. |

In the run, `hub.team_run` is true for the declared schedule, a webhook and a
Settings or CLI run (the workflow's own account); false for Run for me and
personal schedules. `hub.trigger` and `hub.started_by` say how it started.

**Access.** Each `as_tool` workflow adds a row to the agent's Access screen
under Hubzoid tools → Workflows: **Run Program report** (`wf_program_report`).
**Schedule reports for themselves** (`schedule_reports`) lets a person save
schedules, and **Share reports with the company domain** (`share_domain_links`,
sensitive) lets a run publish with `audience="domain"`. Nobody has them until
an admin grants them.

**Live reports.** `key=` makes the artifact a copy in a report series with one
link, `latest_url`, that always shows the newest copy. A team run writes the
agent's series (`/portal/latest/<agent>/<key>`); Run for me and personal
schedules write that person's own series, private to them until they share it
from the viewer. A key belongs to one workflow. When the file, or `digest=` (a
hash of the data behind it), matches the newest copy and sharing policy, nothing new is stored. A
scheduled team run that no longer publishes a key marks that page "No longer
updating". `series_keep: 50` in `workflows/settings.yaml` keeps the last 50
copies of each series. See [reports-and-email.md](reports-and-email.md#live-reports).

**Personal schedules.** In chat: "run the program report for me every
Monday at 9". The agent shows what will run, as whom and the next three runs,
and saves it only after a yes (a one-time code, valid 10 minutes). The first
run starts at once. Each run acts as that person, so it stops when their access
does. The schedule pauses after three failures in a row, or when access or
inputs change. Nobody is emailed unless the hub sets `notify_by_email: true` in
`workflows/settings.yaml` and email delivery is set up; then the owner hears when
a run changes the report, fails or pauses, and when a long Run for me finishes.
"My schedules", "pause", "resume", "run now", "change to daily" and "stop" work
in chat, and in the web app's **Settings → Schedules**, which the agent's "my
schedules" reply links everywhere. It lists their own
schedules and report links on every agent, each with its latest runs. In 1.3.1,
**Edit** changes saved inputs and timing, and **Recent requests** shows completed
one-off answers and files without workflow-administration access. The report
catalog lists permitted reports with a suggested chat request. Admins
also get **Everyone**: each person's schedules with their 30-day model cost, to
pause or stop; only an admin resumes what an admin paused. Limits, in `workflows/settings.yaml`:

```yaml
personal_schedules:
  min_interval: 1h        # shortest cadence
  per_person: 10          # schedules per person per agent
  pause_after_failures: 3
```

**Testing without the engine:**

```python
from hubzoid.workflows.testing import run_local

out = run_local(program_report, hub_dir="my-hub", inputs={"program": "Inner Engineering"},
                as_user="priya@company.org")
out.result, out.artifacts, out.emails      # nothing is published or sent
```

`run_local` reads `workflows/settings.yaml`, so `hub.setting()` works as in the engine.

## Watching it

- `hubzoid schedule status <hub>`: definitions, recent runs and errors.
- Each agent's **Runs & schedules** tab in Settings: its runs, steps and errors.
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

A body that is a JSON array is refused with 422. Zoho Desk's built-in webhooks
send an array of events and sign it with a JWT (`X-ZDesk-JWT`), which Hubzoid
does not verify, so use a Zoho Desk workflow rule with a custom function that
POSTs a JSON object with the secret header instead. One rule can run on
**Create** and another on **Customer Reply**. Put what changes per occurrence
in the body and in `event_key`, for example the trigger and the time it
fired: a repeat with the same key and body is the same event, so a reply that
sends only the ticket id counts as a repeat of the first delivery and starts
nothing. The legacy single webhook (`WEBHOOK_INBOUND_NAME`) likewise drops an
identical body seen within the last 10 to 20 minutes.

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

Settings shows each webhook under the agent's **Runs & schedules**: its URL,
the workflows it starts, the last 24 hours of events by state, and the latest
failures with the redrive command to run on the server. It never shows payloads,
headers or digests, and it has no redrive button.

Inspect deliveries with `hubzoid schedule deliveries <hub>`. Failed events can
be retried explicitly with `hubzoid schedule redrive <event-id> --hub <hub>` after
checking side effects. Cancelling a webhook run from Settings fails its event
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

One case is not recovered automatically. Two processes must serve the same hub
on PostgreSQL. The old owner must be unable to run for longer than the lease (a
paused virtual machine or a long network partition) while its database session
ends, and it must claim queued work the moment it resumes, before it notices
the loss. Those runs then stay pending, without side effects, and hold their
workflow's queue until the owning bridge restarts, which queues them again.
A frozen process alone keeps its session and its lock, so it cannot be taken
over. Run one bridge per hub, and restart the owning bridge if runs stay
pending after such an outage.

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
Runs started from Settings or a webhook neither count toward that nor reset it. Set
`pause_after_failures: 0` to disable auto-pause. Webhook events are not paused.
Resume from Settings or `hubzoid schedule resume` after fixing the cause.

The engine checks for finished runs every 30 seconds. It reads only runs that
finished since its last check, by completion time, from a cursor kept in the
operational store, so history size does not slow it down. On its very first
start (a hub new to 1.1) it looks back 24 hours only: runs that finished
before then are never alerted, and there is no historical backfill. From then
on the cursor catches up downtime of any length.

Alerts are off until you configure a destination. Destinations under `alerts.to`
accept `webhook: ENV_VAR`, `slack: ENV_VAR` (incoming webhook), and
`email: address`. Email uses the existing deployment SMTP settings. Preview mode
writes private `.hubzoid/alert-preview/` files. `@workflow(alert_to=...)` or a
markdown task's `alert_to:` overrides the hub destinations. `HUBZOID_ALERT_URL`
is the deployment fallback. With no destination at all, nothing is recorded or
sent. `alerts: {to: []}` (or `alert_to=[]`) turns alerts off for a hub or
workflow even when the fallback is set. Failed runs always show in Settings, Activity.
Exhausted deliveries are visible in `schedule deliveries`. They do not disappear
as successful sends.

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
is JSON with `hub`, `kind`, `url` (a Settings link) and, when they apply,
`workflow`, `run_id` and `count`. Private outputs and raw exception text stay
out. Slack and email receivers may still see a duplicate after an ambiguous
transport failure.

The edge independently watches engine readiness and sends stale and recovered
alerts. `GET /healthz/workflows` returns 503 if a configured hub is unhealthy,
without exposing hub names or errors. Monitor this URL **externally** and
supervise the process: an edge cannot report its own host's death. Bridge
liveness remains separate from workflow health. This endpoint requires the
edge. A standalone headless bridge should be monitored by its supervisor.
