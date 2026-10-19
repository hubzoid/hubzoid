# Isolated agents (stations) and handoffs

A **station** is an agent with one job that a work-item workflow calls: an
analyst that writes a plan, an implementer, a reviewer that checks the plan
independently. It runs alone:

- its own instructions, and nothing from the main agent, its conversation,
  skills or knowledge;
- exactly the tools it lists. No tools listed means no tools;
- its own engine (`claude-local` or `codex-local`) and model;
- a result checked against its JSON Schema, using the engine's own structured
  output (the Claude Agent SDK's `output_format`, Codex's `outputSchema`).

A station is different from a sub-agent loaded as a skill (its instructions
join the main agent) or a delegate the main agent calls in chat. A station is
called by workflow code, for one stage of one work item.

## Declare one

```markdown
<!-- agents/analyst/AGENTS.md -->
---
name: analyst
description: Analyse the supplied policy and save the analysis
isolated: true
engine: claude-local
model: sonnet
tools: [save_handoff]
response_schema: schemas/result.json
---
Analyse only the policy you are given. Identify its exception owner.
Save your analysis with save_handoff (kind analysis). Return a summary and the handoff id.
```

```json
// agents/analyst/schemas/result.json
{"type": "object", "additionalProperties": false,
 "properties": {"summary": {"type": "string"}, "handoff_id": {"type": ["string", "null"]}},
 "required": ["summary", "handoff_id"]}
```

| Key | Meaning |
|---|---|
| `isolated` | `true` makes the agent a station. Without it, the agent keeps today's behaviour. |
| `engine` | `claude-local` or `codex-local`. Default: the hub's runtime. |
| `model` | Optional. The engine's signed-in default when left out. |
| `tools` | The exact tools: the hub's own and pre-shipped tools by name, plus `save_handoff` and `read_handoff`. Empty or missing means none. |
| `response_schema` | A JSON Schema file in the agent's folder, describing an object. |
| `input` | `forward` (default) or `never`. |
| `sandbox` | A sandbox profile for the agent's workspace. Sandboxes are not in this version yet: a station that names one is refused (`sandbox_unavailable`), never run on the host. |
| `timeout_seconds` | Optional limit on active time, across all attempts of a call. Off by default. |

Changing a station's files gives it a new definition digest. A call keeps the
definition it started with, even if the files change or are removed later. It
also pins the tools it lists: if one of them changes before a retry or a
recovery, that call stops with `definition_changed` rather than run different
code. Restore the tool or use a new stage revision.

## Call it from a work item

```python
import json
from hubzoid import hub, workflow
from hubzoid.stations import StationOutputError
from hubzoid.work_items import Route

def ask(message, agent, stage):
    try:
        return hub.call_agent(message, agent=agent, stage=stage)
    except StationOutputError as error:
        return hub.retry_station(error.call_id, "Return exactly summary and handoff_id.")

@workflow(work_items={"plan": Route(stages=("analysis", "review"), actions=())})
def review_policy():
    message = hub.work_item.input["message"]
    analysis = ask(message, "analyst", "analysis")
    return ask(message + "\nAnalyst result: " + json.dumps(analysis), "reviewer", "review")
```

- `hub.call_agent(message, agent=..., stage=...)` runs the station once for that
  stage of the work item and returns its checked result. The call is saved: a
  restart, redrive or continuation returns the saved result without running it
  again. The stage must be in the work item's route.
- A result that does not match the schema raises `StationOutputError`; a failed
  run raises `StationError`. Both carry `call_id`.
- `hub.retry_station(call_id, clarification)` is the one retry the workflow may
  direct, with a clarification added to the original task. A second retry is
  refused (`retry_exhausted`). `orchestrator_retries: 0` turns retries off.
  An answer with no JSON at all is also a `StationOutputError`.
- `hub.call_agent(..., timeout=seconds)` may lower the station's time limit,
  never raise it. The limit counts active time over every attempt of the call.
- If the work stopped on a failed call without using its retry, a person can
  direct that one retry (see below). It is the same allowance: the workflow
  cannot retry again afterwards.
- Before a station starts, and before each of its tool calls, Hubzoid checks
  that the requester's account can still act and that the work was not
  cancelled. After a cancel, its tools refuse to run and its result is not
  used.
- If Hubzoid stops while a station runs, the call is resumed once
  (`recovery_attempts`), separately from the retry. Writes it made through
  `hub.operation.run` are recorded, so they are not repeated.

## Handoffs

A handoff is a document one station saves for another station of the same work
item, so a long analysis passes by id instead of being pasted into a message.
`save_handoff(kind, title, markdown)` returns `{id, kind, title, sha256,
character_count}`. `read_handoff(id)` returns it with its Markdown. Handoffs
cannot be changed, and an id from another work item reads as not found.
Workflow code can use `hub.save_handoff(...)` and `hub.read_handoff(id)` too.

## Settings

Optional, in `work-items.yaml`:

```yaml
version: 1
work_items:
  stations:
    orchestrator_retries: 1   # 0 or 1
    recovery_attempts: 1      # 0 or 1
    timeout_seconds: null     # off, or 1 to 86400 of active time per call
  handoffs:
    kinds: [analysis, plan, review]   # any lowercase slug when left out
    max_characters: 200000
```

## Check and inspect

```bash
hubzoid stations validate ./hub
hubzoid stations doctor ./hub
hubzoid stations calls list ./hub --work-item ITEM
hubzoid stations calls show ./hub --work-item ITEM --call CALL
hubzoid stations calls retry ./hub --work-item ITEM --call CALL --clarification-file fix.txt --expected-revision N
hubzoid handoffs list ./hub --work-item ITEM
hubzoid handoffs read ./hub --work-item ITEM --id ID
```

`doctor` shows each station's digest, engine, model, exact tools, whether its
output is structured, and whether its engine's login is available. It never
prints a credential. The Console's Work items page shows each work item's
station calls, their attempts, results or errors, and its handoffs. The API is
`GET /portal/api/hubs/{hub}/work-items/{id}/stations`,
`.../handoffs/{handoff_id}` and `POST .../stations/{call_id}/retry` with
`{clarification, expected_revision}`. A retry is for the person who asked for
the work or a manager, only once the work has stopped. The work continues from
that call.

## Limits

- `allow_generic_subagents` and `max_transcript_bytes` are refused in
  `work-items.yaml` until they have an effect.
- Questions or approvals from inside a station turn are not forwarded to a
  person yet. Write steps that need approval as `hub.operation.run` calls in the
  workflow, which wait for the requester.
- `codex-local` requires the Codex CLI version the SDK pins
  (`SUPPORTED_CODEX_VERSION`).
