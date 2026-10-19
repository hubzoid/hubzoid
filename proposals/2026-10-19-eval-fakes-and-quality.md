# Eval fake tools, subfolders, JSON and JUnit, and required evals in doctor

Status: proposed for 2.0. Covers GitHub #58 (JSON for eval commands) and
discussion #62 (dummy tool results).

## Problem

- **Evals run the hub's real tools with real credentials.** The behaviour that
  matters most is the agent deciding to send, refund or post, and that is
  exactly what a hub author cannot test safely. The docs advise read-only
  prompts.
- **Suites cannot be organised.** Cases must sit flat in `evals/`.
- **CI and assistants can't read results.** CI systems want JUnit. Scripts
  and coding assistants want JSON from `eval list`, `status` and `explain`.
- **Doctor knows nothing about evals.** A hub cannot say which evals must pass
  before it is ready. A result from before a rule edit still looks green.

## What should happen

- **Fake tool results.**
  - A case declares `fake_tools`: a tool name, optional matching arguments,
    and an output or an error.
  - Inside that case, matching calls of the hub's own tools get the fake, and
    `real_tools` may run for real. Every other call is blocked: nothing runs,
    the agent reads that it was blocked, and the case fails naming the call.
  - Fakes apply after the access check, so a refused person stays refused.
  - MCP servers are outside this layer, so a faked case on a hub with MCP
    servers fails unless it says `allow_real_mcp: true`.
  - Outside a faked case nothing changes.
- **Subfolders.** Cases may live in subfolders. A case's id is its path under
  `evals/` without `.md`. `_`, `.` and `fixtures/` are not cases.
- **Machine-readable output.**
  - `eval run --json` prints the run record, and `--junit FILE` writes JUnit
    XML.
  - `eval list`, `eval status` and `eval explain` take `--json`.
- **Required evals in doctor.**
  - `quality/manifest.yaml` lists the cases (ids, globs or tags) a hub
    requires.
  - Doctor shows one row per required case: `ok`, `fail`, or `pending` when
    it never ran or the case, or the hub's instructions, tools or settings,
    changed since its last result.
  - Each result records the digest of its case file, and each run the digest
    of the hub.

## Scope and non-goals

- No new eval framework. The existing runner, report files and Console tab
  stay.
- No approval, question or plan-review steps, and no assertions on work items
  or channels yet. Those need the request, work-item and channel services.
- No live provider test accounts.

## Definition of done

- A faked case that calls an unfaked tool fails, and the tool never runs. A
  fully faked case passes with zero real calls.
- Nested cases list, run and filter by id.
- JUnit and JSON output are stable and tested.
- Doctor reports a required case as pending after its case or its hub
  changes, and ok again after a fresh passing run.
- The same faked case works on a real model run with the installed runtime.
