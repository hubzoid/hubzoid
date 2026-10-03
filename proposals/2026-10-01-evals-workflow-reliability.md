# Evals and dependable event workflows

## Problem and agreement
Teams need inspectable multi-turn evals and webhook-triggered code workflows that
survive duplicate delivery, crashes and bounded provider failures. The maintainer
approved implementation on 2026-10-01 following the plan review. This proposal
records the corrected contract before implementation. All changes remain local.

## Scope
Integrate the native chat app, activity, administration and usage-limit branches.
Then implement workflow ownership/readiness, named authenticated webhooks,
per-workflow and per-key concurrency, model/agent deadlines, durable alerts,
health monitoring, and eval v2 with a read-only Console tab. Preserve existing
hub files, optional fields, runtime-neutral seams and legacy inbound webhooks.
Approvals, human waiting, a Console eval run button and memory changes are out.

## Reliability contract
An event is identified by hub, webhook and sender event key, independently of
workflow renames. Acceptance means a committed event row. A reconciler enqueues
one deterministic DBOS coordinator; retries use unique child attempt IDs. Digests
are compared at admission even for completed events. Terminal failures remain
inspectable and can be redriven explicitly. No exactly-once external action claim.

DBOS remains the execution/checkpoint engine. Code webhooks are received by the
existing bridge, avoiding a second process writing its SQLite workflow database.
Legacy chat/inbox inbound surfaces remain separate. Coordinators are async, so
waiting for sync children cannot exhaust their executor pool. Sync run deadlines
never release ticket exclusion before the thread exits. Calls are bounded;
arbitrary author code must supply its own I/O deadlines. Cancellation cannot
undo an external action and remote server processing may continue after a client
connection is closed. External writes require idempotency at their destination.

A lifetime lock prevents two local owners (file lock on SQLite, session advisory
lock on PostgreSQL). Readiness also has a boot ID, fencing generation and expiry.
Expiry is a health signal, not permission to start alongside a locked owner.
An owner losing its database session fails closed. A code upgrade explicitly
fails old-version accepted work rather than silently cancelling or replaying it
on changed code; unstarted events are dispatched against the current mapping.

Eval files are private, versioned, redacted, uniquely named and atomically
written. The index is rebuildable and updated under the same lock as retention.
Account-scoped details are visible only to that account, after agent access is
checked. Other authorized viewers receive verdict/count summaries only.

## Build and validation
Five branches: foundations, webhooks, deadlines, alerts and evals-v2. Webhooks
and deadlines consume merged foundations; alerts consumes both and integrates
eval failure semantics. Verify with model-free unit/integration tests, SQLite
crash/duplicate/ownership/saturation tests, PostgreSQL when a test service is
available, Console and chat journeys, package build, first-use checks and a
final independent Opus 5.5 review. Never use customer hubs for test execution.

## Provider and operational boundaries
Generic shared-secret and timestamped HMAC are supported explicitly. A provider
adapter must have a verified signing contract before being advertised. A host's
own death requires an external monitor of the workflow health endpoint. Alert
email requires configured delivery; missing destinations remain an observable
configuration problem. No mandatory new service is introduced for first use.
