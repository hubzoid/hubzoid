# Work items and safe external writes

A **work item** is one piece of work someone asked for: "triage issue 42",
"reply to this email", "draft the weekly report". It has a stable key, a route
of stages it may run, and a state that survives restarts, redeliveries and
upgrades. Finished stages are saved, so they are never run or paid for twice.

An **operation** is one action a work item takes outside the hub: sending an
email, posting a comment, submitting an ERP document. Each is recorded before
anything is sent, and an uncertain send is never repeated on its own.

Ordinary chat replies are not work items. A chat answer interrupted by a
restart is gone. Use a work item for work that must finish, or must not happen
twice.

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

- `work_items=` is `True` (one route named `default`, no limits), or a mapping
  of route names to `Route(stages=..., actions=...)`. `None` in a route means
  no limit. A stage or action outside the item's route is refused before it
  runs (`scope_exceeded`).
- `hub.work_item.checkpoint(stage, fn, response_model=None)` runs `fn` once
  and saves its output. `response_model` (Pydantic) validates it.
- `hub.work_item.input` is what the work was started with.
- `hub.work_item.messages()` returns follow-ups people sent since the last call.
- `hub.work_item.reserve(counter, key, limit)` counts something once per key,
  such as review rounds, and returns `None` past the limit.
- `hub.work_item.narrow(route)` moves the work to a narrower route. Nothing
  inside a run can widen it.

Call operations between stages, not inside a stage function. If an operation
waits for a person inside a stage, that stage runs again when the work
continues.

Inside a work item every operation names the actions it performs
(`actions="github.comment"`), and the item's route must allow them. The
consequential actions (`email.send`, `erp.submit`, `customer_reply.send`, and any
a hub registers with `hubzoid.work_items.config.register_action`) also follow
their write mode. Approval needs a work item: outside one, an `approve` action is
refused, and an uncertain outcome stops the run and stays recorded for a person.

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
| `idempotent` | Sending the same request again is safe. |
| `provider_key` | Send again with the same provider key, while the provider honours it (`provider_key_ttl` seconds). |
| `reconcile` | A read-only `reconcile(arguments)` check returns `Reconciled("applied", result)`, `Reconciled("safe_to_retry")` or `Reconciled("unknown")`. An empty search result is not proof. |
| `escalate` | Never guess. The operation becomes ambiguous. |

An **ambiguous** operation stops the work (`needs_input`). Only the person who
asked for the work decides:

- **Done**: it happened. Evidence is required (a link, an id, a note).
- **Retry**: try again, accepting it may happen twice.
- **Abandon**: stop the work.

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

- `off` (the default for any action not listed): the action is not offered.
- `draft`: only a real draft in the target system, which a person finishes
  there. An action without a draft adapter cannot use it.
- `approve`: sent only after the person who asked for the work approves the
  exact content. Before sending, Hubzoid checks the digest again, the
  approver's current access and, when the adapter passes `target_version`, that
  the target did not change. Any difference needs a fresh approval.

Nothing is sent automatically. When a write performs several actions, the
strictest mode wins. Quote `"off"`: plain YAML reads `off` as `false`.

Waiting for approval holds no thread. The work parks, and continues from its
last saved step once approved.

## Start, watch and decide

```bash
hubzoid work-items validate ./desk
hubzoid work-items start ./desk --workflow desk --key operator:demo-1 --route act --input request.json
hubzoid work-items list ./desk
hubzoid work-items show ./desk ITEM
hubzoid operations show ./desk OPERATION
hubzoid operations approve ./desk OPERATION --digest sha256:...
hubzoid operations deny ./desk OPERATION --reason "Wrong customer"
hubzoid operations resolve ./desk OPERATION --decision done --evidence "Message 4821 in Sent"
hubzoid work-items cancel ./desk ITEM
hubzoid work-items restart ./desk ITEM --reason "Fixed the template"
```

Starting the same key with the same input returns the existing item. Different
input under an active key is refused (`key_conflict`). Use `--new-generation`
for new work once the old one finished. A webhook declared for a work-item
workflow admits each delivery once under its event key, so a redelivery or a
redrive finds the same item.

Every command takes `--json`. Exit codes: 0 done, 1 failed, 2 invalid input,
3 denied, 4 conflict, 5 unavailable, 6 waiting for a person or uncertain.
`start --as EMAIL` records who asked for the work (default: the hub's workflow
account). Approve, deny and resolve belong to that person, so the CLI makes
them only in local mode with sign-in off, as the machine's single account
(`admin@localhost`). Elsewhere, use Settings or the API signed in as that
person.

The same actions are HTTP routes under `/portal/api/hubs/{hub}/`:
`work-items` (GET, POST with `Idempotency-Key`), `work-items/{id}`,
`work-items/{id}/messages`, `cancel`, `resume`, `restart`, `operations`,
`operations/{id}`, `operations/{id}/approve`, `deny` and `resolve`. Changes
accept `expected_revision`. Errors are `{"detail": {"code", "message"}}`.

The agent's **Work** tab in Settings shows the same thing: state, saved stages,
operations, the exact content waiting for approval, and the decision form.

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
(`definition_changed`) instead of continuing on new code. Check what changed, then restart it:
`hubzoid work-items restart HUB ITEM --reason "..."` continues after the last
saved stage. `--stage NAME` re-runs that stage and everything after it. Earlier
stages are reused. Every uncertain operation must be decided first. There are
no automatic stage migrations.

## Upgrading

Existing hubs are unchanged: nothing runs as a work item until a workflow
declares `work_items=`, and no action is gated until `work-items.yaml` names it.
The upgrade adds three tables (`hz_work_items`, `hz_operations`,
`hz_idempotency`) and changes no existing row. Earlier email deliveries keep
their own records and are never resent. Back up the operational database and
the hub's workflow database together.
