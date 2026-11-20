# Work items and safe external writes

A **work item** is one piece of work someone asked for: "triage issue 42",
"reply to this email", "draft the weekly report". It has a stable key, a route
of stages it may run, and a state that survives restarts, redeliveries and
upgrades. Finished stages are saved, so they are never run or paid for twice.

An **operation** is one action a work item takes outside the hub: sending an
email, posting a comment, submitting an ERP document. Each is recorded before
anything is sent, and an uncertain send is never repeated on its own.

Use a work item for work that must finish, or must not happen twice. Ordinary
chat replies are not work items. A chat answer interrupted by a restart is gone.

## Write one

```python
# workflows/desk/main.py
from hubzoid import hub, workflow
from hubzoid.work_items import Route

@workflow(work_items={
    "answer": Route(stages=("plan",), actions=()),
    "act": Route(stages=("plan", "check"), actions=("email.send",)),
})
def desk():
    plan = hub.work_item.checkpoint("plan", lambda: draft_reply(hub.work_item.input))
    if not hub.work_item.allows("check"):
        return plan
    hub.work_item.checkpoint("check", lambda: check_reply(plan))
    sent = hub.operation.run(
        "send_reply",
        target={"thread": plan["thread"]},
        arguments={"to": plan["to"], "body": plan["body"]},
        recovery="escalate",
        actions="email.send",
        dispatch=send_reply,       # sends
        draft=save_reply_draft,    # saves a real draft in the mailbox
    )
    return {"plan": plan, "sent": sent}
```

| Call | What it does |
|---|---|
| `@workflow(work_items=...)` | `True` (one route named `default`, no limits), or route names to `Route(stages=..., actions=...)`. `None` in a route means no limit |
| `hub.work_item.input` | What the work was started with |
| `hub.work_item.allows(stage)` | Whether the item's route includes this stage |
| `hub.work_item.checkpoint(stage, fn, response_model=None)` | Runs `fn` once and saves its output. `response_model` (Pydantic) validates it |
| `hub.work_item.messages()` | Follow-ups people sent since the last call |
| `hub.work_item.reserve(counter, key, limit)` | Counts something once per key, such as review rounds. Returns `None` past the limit |
| `hub.work_item.narrow(route)` | Moves the work to a narrower route. Nothing inside a run can widen it |
| `hub.operation.run(name, ...)` | Records and sends one outside action ([below](#operations-and-recovery)) |

A stage or action outside the item's route is refused before it runs
(`scope_exceeded`).

Call operations between stages, not inside a stage function. If an operation
waits for a person inside a stage, that stage runs again when the work
continues.

Inside a work item every operation names the actions it performs
(`actions="github.comment"`), and the item's route must allow them. The
consequential actions also follow their [write mode](#write-modes):
`email.send`, `erp.submit`, `customer_reply.send`, `github.pr_update`, and any
a hub registers with `hubzoid.work_items.config.register_action`. Approval
needs a work item. Outside one, an `approve` action is refused, and an
uncertain outcome stops the run and stays recorded for a person.

## Operations and recovery

`hub.operation.run(name, target=..., arguments=..., recovery=..., dispatch=...)`
records the exact intent (its `sha256:` digest) before sending. The same name
and target with different arguments in the same stage is refused
(`operation_argument_conflict`). Pass `key=` for a deliberate second action.

Your `dispatch` function returns a JSON result. Raise
`hubzoid.work_items.NotSent` when the request provably never left (safe to try
again), or `Rejected` when the other system definitely refused it. Any other
error means the outcome is unknown.

`recovery` says what happens when a send was interrupted:

| Class | Meaning |
|---|---|
| `idempotent` | Sending the same request again is safe |
| `provider_key` | Send again with the same provider key, while the provider honours it (`provider_key_ttl` seconds) |
| `reconcile` | A read-only `reconcile(arguments)` check returns `Reconciled("applied", result)`, `Reconciled("safe_to_retry")` or `Reconciled("unknown")`. An empty search result is not proof |
| `escalate` | Never guess. The operation becomes ambiguous |

An **ambiguous** operation stops the work (`needs_input`). Only the person who
asked for the work decides:

| Decision | Meaning |
|---|---|
| `done` | It happened. Evidence is required (a link, an id, a note) |
| `retry` | Try again, accepting it may happen twice (`--acknowledge-duplicate-risk`, or `acknowledge_duplicate_risk: true` in the API) |
| `abandon` | Stop the work |

## Write modes

`work-items.yaml` at the hub root sets each consequential action:

```yaml
version: 1
write_modes:
  email.send: draft
  erp.submit: "off"
  customer_reply.send: approve
write_mode_overrides:
  whatsapp-desk:
    customer_reply.send: "off"
```

| Mode | What happens |
|---|---|
| `off` | The default for any action not listed. The action is not offered |
| `draft` | Only a real draft in the target system, which a person finishes there. An action without a draft adapter cannot use it |
| `approve` | Sent only after the person who asked for the work approves the exact content |

Before an approved send, Hubzoid checks the digest again, the approver's
current access and, when the adapter passes `target_version`, that the target
did not change. Any difference needs a fresh approval.

Nothing is sent automatically. When a write performs several actions, the
strictest mode wins. Quote `"off"`, because plain YAML reads `off` as `false`.
Waiting for approval holds no thread. The work parks, and continues from its
last saved step once approved.

## Start, watch and decide

```bash
hubzoid work-items validate ./desk
hubzoid work-items start ./desk --workflow desk --key operator:demo-1 --route act --input request.json
hubzoid work-items list ./desk
hubzoid work-items show ./desk ITEM
hubzoid work-items message ./desk ITEM --text "Use the new address"
hubzoid operations list ./desk
hubzoid operations show ./desk OPERATION
hubzoid operations approve ./desk OPERATION --digest sha256:...
hubzoid operations deny ./desk OPERATION --reason "Wrong customer"
hubzoid operations resolve ./desk OPERATION --decision done --evidence "Message 4821 in Sent"
hubzoid work-items cancel ./desk ITEM
hubzoid work-items resume ./desk ITEM
hubzoid work-items restart ./desk ITEM --reason "Fixed the template"
hubzoid work-items config ./desk
```

- Starting the same key with the same input returns the existing item.
  Different input under an active key is refused (`key_conflict`). Use
  `--new-generation` for new work once the old one finished.
- A webhook declared for a work-item workflow admits each delivery once under
  its event key, so a redelivery or a redrive finds the same item.
- `start --as EMAIL` records who asked for the work. The default is the hub's
  workflow account.
- Approve, deny and resolve belong to the person who asked. The CLI makes them
  only in local mode with sign-in off, as the machine's single account
  (`admin@localhost`). Elsewhere, decide in Settings or through the API as that
  person.

Every command takes `--json`.

| Exit code | Meaning |
|---|---|
| 0 | Done |
| 1 | Failed |
| 2 | Invalid input or not found |
| 3 | Denied |
| 4 | Conflict |
| 5 | Unavailable or rate limited |
| 6 | Waiting for a person, or uncertain |

**In Settings**, My work items lists the work you asked for and what waits on
you, with the exact content waiting for approval and the decision form. Work
items shows the items of an agent: state, saved stages and operations.

## HTTP routes

The same actions are routes under `/portal/api/hubs/{hub}/`, with a Settings
session, a personal API key or an API app's machine token.

| Route | What |
|---|---|
| `GET work-items`, `POST work-items` | List, or start with an `Idempotency-Key` header (`202` new, `200` existing) |
| `GET work-items/config` | The active `work-items.yaml` and its revision |
| `GET work-items/{id}` | One item |
| `POST work-items/{id}/messages` | Send a follow-up |
| `POST work-items/{id}/cancel`, `resume`, `restart` | Change its state |
| `GET work-items/{id}/operations` | Its operations |
| `GET operations`, `GET operations/{id}` | Operations you may see |
| `POST operations/{id}/approve` | Body `{digest}` |
| `POST operations/{id}/deny` | Body `{reason}` |
| `POST operations/{id}/resolve` | Body `{decision, evidence, acknowledge_duplicate_risk}` |

Changes accept `expected_revision`. Errors are `{"detail": {"code", "message"}}`.

Only the `operations` routes are in the [published API reference](api.md#stability),
as beta. The other work-item routes may change in any release.

### Decide with an API key

A key reaches `approve`, `deny` and `resolve` only when it holds the `approve`
operation, and only for work its owner asked for. The decision records the key.
A machine token from an API app never decides.

```bash
hubzoid auth keys create "Desk approvals" --operations read,approve --days 30
curl -s -X POST https://hub.example.com/portal/api/hubs/desk/operations/$OP/approve \
  -H "Authorization: Bearer $APPROVE_KEY" -H "Content-Type: application/json" \
  -d '{"digest": "'"$DIGEST"'"}'
```

## Who may do what

- Starting work needs the workflow's own grant (`wf_<name>`) or Run and control
  workflows on the agent.
- The person who asked for the work sees everything about it and is the only
  one who approves, denies or resolves its operations. An administrator cannot
  approve for them.
- Managers (See or Run and control workflows) see the state of every item, not
  its input, result or operation content. They can cancel and restart.

## Code changes and restarts

Work belongs to the workflow code that started it. If the code changes while
work is interrupted or waiting for a person, the work stops as `needs_input`
(`definition_changed`) instead of continuing on new code. Check what changed,
then restart it:

- `hubzoid work-items restart HUB ITEM --reason "..."` continues after the last
  saved stage.
- `--stage NAME` reruns that stage and everything after it. Earlier stages are
  reused.
- Every uncertain operation must be decided first.

There are no automatic stage migrations.

## Upgrading

Existing hubs are unchanged. Nothing runs as a work item until a workflow
declares `work_items=`, and no action is gated until `work-items.yaml` names it.
The upgrade adds three tables (`hz_work_items`, `hz_operations`,
`hz_idempotency`) and changes no existing row. Earlier email deliveries keep
their own records and are never resent. Back up the operational database and
the hub's workflow database together.
