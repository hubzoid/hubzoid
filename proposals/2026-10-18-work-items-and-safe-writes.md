# Durable work items and safe external writes

Status: implementation authorized by the owner on 18 October 2026, for 2.0.

## Problem

A workflow that does real work for someone (triage an issue, draft a reply,
open a pull request) has no record of that work as one thing. Today:

- A webhook retry or redrive starts a new engine run with a new id, so stages
  that already finished run again and are paid for again
  (`hubzoid/workflows/events.py`, the coordinator's per-attempt run ids).
- A step that sends something outside the hub can run twice after a crash.
  The engine is at-least-once (`hubzoid/workflows/runtime.py` module notes).
  Only the email outbox records an intent and an "ambiguous" outcome
  (`hubzoid/email_delivery.py`). Nothing else does.
- There is no way to say "this action may only save a draft" or "a person must
  approve this exact message before it is sent".
- A run interrupted across a code change is cancelled at the next start, with
  no signal to the person who asked for the work.

## Why now

The ready-made hubs that send customer email, post to an ERP or publish code
need these guarantees before anyone can trust them unattended.

## What should happen

Keep it thin. The workflow engine (DBOS) already gives most of the durability,
so Hubzoid uses it rather than rebuilding it:

| Need | Provided by |
|---|---|
| A duplicate start returns the same run | DBOS workflow ids |
| Finished stages are never re-run | DBOS steps |
| One execution per work item at a time | A DBOS queue partitioned by work item |
| Messages to running work | DBOS `send` and `recv` |
| Active work stays on its own code | DBOS application versions |
| Continue from a saved stage | DBOS `fork_workflow` |

Hubzoid adds only what DBOS does not do:

1. **Work items.** One record per piece of requested work: a logical key
   (`github:42#7`, `operator:demo-1`), a generation, the route it may follow,
   its state and outcome. Repeating a start with the same key and input returns
   the same item. A new generation is only ever explicit.
2. **Operations.** Every consequential external write goes through
   `hub.operation.run(...)`. It records the exact intent and a digest before
   anything is sent, and the outcome after. Each operation declares how an
   interrupted send is recovered: `idempotent`, `provider_key`, `reconcile`
   (with a read-only check) or `escalate`. When the outcome cannot be known,
   the operation is `ambiguous`, the work stops, and only the person who asked
   for the work can resolve it as done, retry (accepting duplicate risk) or
   abandon. Changed arguments under an existing key are refused.
3. **Write modes.** A new optional hub file, `work-items.yaml`, sets each
   consequential action to `off` (not offered), `draft` (only a real draft in
   the target system) or `approve` (sent only after a person approves the
   exact content, bound by a `sha256:` digest). Nothing is sent automatically.
   At dispatch the payload digest, the approver's current access and, where an
   adapter supports it, the target's version are checked again.
4. **Waiting without holding a thread.** Work that needs a person (an approval
   or an ambiguous outcome) parks, and continues from its last saved step when
   the person answers.
5. **Tool classes and one credential scrub.** Each tool has a local class
   (`read`, `sandbox_local`, `source_push`, `handoff`, `external_write`,
   `shared_knowledge`, `personal_store`). Unknown tools are treated as external
   writes. One shared scrub removes credentials from operation errors and
   evidence, replacing the separate redactors.
6. **Every action through files, CLI and API.** `hubzoid work-items ...` and
   `hubzoid operations ...`, the matching HTTP routes, and a Console screen that
   uses those routes.

Existing hubs are unchanged until a workflow opts in with
`@workflow(work_items=...)` or the hub adds `work-items.yaml`.

## Scope and non-goals

- No second workflow engine, no custom stage or inbox tables. DBOS holds them.
- No automatic sends and no automatic stage migrations.
- Approval requests in this change are a small adapter with the shape a full
  approvals service will provide later. Approval inside a single agent turn is
  out of scope here.
- Isolated agents, sandboxes and budgets are separate changes that build on
  these records.

## Open questions

- Run identity, permission keys and the shared approvals service are being
  built separately. This change uses today's access model and records where
  those services plug in.

## Definition of done

- Starting the same work twice gives one item. A redrive or restart after a
  finished stage does not call the model for that stage again.
- A crash after an outside system accepted a write never sends it again
  silently. The item shows the operation as ambiguous until its requester
  resolves it.
- `off` hides the action, `draft` saves only a draft, and `approve` sends only
  the exact content a person approved, once.
- Everything above works from the CLI, the API and the Console, with focused
  tests on SQLite and the same tests running on PostgreSQL in CI.
