# Release-gate evals and agent guardrails

Plan, 20 November 2026, on branch `feat/release-gate-guardrails` (from `feat/mcp-parity` at
`f3fd8692`, the 2.0.9 release branch). Founder approved three items after comparing Mastra and
Agno: A (`eval run --required`), E (CI and rollback docs) and F (guardrails). The research and
the items that were cut are in HubzoidAgent `docs/sdk-cicd-release-gate-suggestions-2026-11-20.md`.

## Problem

* A hub's required evals live in `quality/manifest.yaml`, and only `hubzoid doctor` reads it.
  CI has to repeat the selection with `--tag` or `--case`, which drifts from the manifest. An
  empty selection exits 0, so a typo turns the gate green.
* A results file does not say which Hubzoid version or hub commit it tested.
* There is no copy-paste CI file and no single page on rolling a hub back.
* Nothing checks what people send to an agent. Hubzoid limits damage (access guard, approvals,
  tool classes), but has no prompt-injection check, moderation, or personal-data masking. Mastra
  (`PromptInjectionDetector`, `ModerationProcessor`, `PIIDetector`) and Agno
  (`PromptInjectionGuardrail`, `OpenAIModerationGuardrail`, `PIIDetectionGuardrail`) both ship
  these three. Isha's AI governance brief asks for prompt-injection controls.

## Why now

Isha's security brief asks for a release gate and prompt-injection controls, and the founder
wants Hubzoid on par with Mastra and Agno for guards both of them ship.

## What should happen

### A. `hubzoid eval run --required`

One CI command: run exactly the cases `quality/manifest.yaml` requires, fail on any failure or
gap.

* Selects `required` (ids or globs) and `required_tags` through the existing
  `quality.required()`, so it can never select differently from doctor.
* Setup problems exit **2**, before any model call:
  * no `quality/manifest.yaml`, or one that requires nothing;
  * a manifest error, or a requirement that matches no case (`quality.required` already raises);
  * a case file that cannot be read;
  * `--required` together with `--tag`, `--case` or `--no-judge` (each would check less than the
    manifest asks);
  * a required case that is disabled (`enabled: false`);
  * a required case that checks nothing (no `contains`, `not_contains`, `expect_tools`,
    `forbid_tools`, `expect_tool_args` or `## Criteria`). It would pass whenever the agent does
    not crash. Agno refuses such a case for the same reason.
* Any failed case exits **1**. All passed exits **0**.
* Other options work as today: `--junit`, `--json`, `--judge-model`, `--model`, `--run-as`,
  `--quiet`, `--details`, `--compare`.
* The run is an ordinary results file, so `hubzoid doctor` afterwards shows the cases as `ok`.
* Every results file (any trigger) also records `hubzoid_version` and, when the hub folder is in
  a Git work tree, `git_commit` and `git_dirty`. Optional fields: older files and older readers
  are unaffected. JUnit gets them as `<properties>`.

No Console change: the Evals screen is read-only and keeps showing results as today.

### E. Docs

* `docs/ci.md` and `examples/ci/hubzoid-checks.yml`: a GitHub Actions file that installs the
  hub's requirements, runs `pytest` when the hub has a `tests/` folder, then
  `hubzoid eval run --required --junit`, uploads the JUnit file and results as artifacts, and
  explains the model credential (a repository secret, never in the hub) and that pull requests
  from forks get no secrets.
* `docs/rollback.md`: going back one release in one page, pulling together the existing
  `UPGRADING.md` "Going back" and `BACKUP.md` restore steps, hub-change undo, what happens to
  running workflows across a version change (as `workflows.md` already documents), and a short
  rehearsal checklist. Links, not copies, where the detail already lives.
* `docs/evals.md` gains `--required` and the provenance fields.

### F. Guardrails

Set on the main agent, in its `AGENTS.md` frontmatter, like Mastra's per-agent processors:

```yaml
---
name: support-desk
guardrails:
  prompt_injection: block          # off | warn | block
  moderation: off                  # off | warn | block
  personal_data: redact            # off | warn | redact | block
  personal_data_types: [email, phone, card]
  personal_data_patterns:          # the hub's own extra patterns, name: regex
    internal-id: 'INT-\d{6}'
  threshold: 0.7                   # model-based checks: score at or above this is flagged
  model: haiku                     # optional, the model that runs the checks
  on_error: allow                  # allow | block, when the checking model fails
---
```

No `guardrails:` key means no checks, exactly as today. YAML reads a bare `off` as false, which
is accepted as `off`. Unknown keys and values are refused with the file named. Sub-agents receive
work from the main agent, not from people, so `guardrails:` on a sub-agent is refused with a clear
message.

**Where.** `runtime.build()` attaches the guard to the runtime it returns, around that runtime's
own `stream_events`, which `run` and `stream` already go through on every backend (OpenAI, Claude,
Codex, test). The object's type does not change, so `isinstance` checks keep working. The server's
runtime serves web chat, channels (Slack, WhatsApp, Telegram, email intake), the API and MCP, so
all of them are covered, and evals and `hubzoid test` too. Builds for scheduled tasks
(`schedule_runner`) and workflow agent calls (`run_once`) pass `guardrails=False`: those prompts
are the hub's own instructions. Stations and doctor's reply check do not go through
`runtime.build`'s chat path. Doctor's build check keeps the guard, so a bad `guardrails:` value
shows as a failed `runtime.build` row. A nested `stream_events` call in
the same turn (Claude's full-history retry) is not checked twice.

**What is checked.** Only the person's new message, not the whole history, so one flagged
message does not block every later turn. Entry points that flatten history into the prompt name
the new message: the chat engine sets it on the turn's `SessionSlot` (it already has the stored
message), the API sets it from the last `user` message, and MCP's sub-agent delegate sets it to
the task. Without one, the whole prompt is the new message (evals, `hubzoid test`).

**Prompt injection and moderation.** One tool-free call through `runtime.complete_once` with a
JSON schema, scoring `prompt_injection` (injection, jailbreak, system override, role
manipulation, attempts to extract tools) and `moderation` (hate, harassment, violence, sexual,
self-harm), each 0 to 1. Only enabled checks are asked for. The message goes inside delimiters
and the instructions say it is data to classify, never to follow. Model: `model:` if set, else
`haiku` on a Claude hub (claude-local or Bedrock), else the hub's own model. It runs on the hub's
own runtime and credentials, so a subscription hub has no separate API spend. Usage is recorded
with kind `guardrail`. On a model failure, `on_error: allow` lets the message through and logs it,
`block` stops it.

**Personal data.** Pattern matching, no model call: email, phone (international or local formats,
at least 10 digits), card numbers (13 to 19 digits that pass the Luhn check), plus the hub's own
patterns. Checked both ways:

* Incoming: `warn` logs, `redact` replaces each match with `[redacted]` in the prompt (and in the
  resume prompt on the turn's `SessionSlot`), `block` stops the message.
* Replies: `warn` logs, `redact` and `block` both replace matches with `[redacted]` as the reply
  streams (a stream cannot be taken back, so replies are masked, not stopped). A short hold-back
  keeps a value split across two chunks whole, as Mastra's regex filter does. Tool events pass
  through in order.

Stored chat history keeps what the person typed. Masking applies to what the model sees and what
the person is shown.

**Blocked.** The agent never runs. The reply is one fixed sentence: "This message was stopped by
the agent's safety check." No model call is spent on the agent.

**Recorded.** Every warn, redact and block writes one row to the access decision log
(`hz_access_decisions`, the Console's Activity page and `hubzoid audit`): tool
`guardrail:<check>`, decision `warn`, `redact` or `block`, reason with the score or the pattern
names. Never the message text. Eval results record the guard decisions of each case.

## Scope and non-goals

* Not checked: emails, files or tool results the agent reads during a run, and the hub's own
  workflow prompts. Mastra and Agno check messages too, not tool results. Access control and
  approvals stay the main protection there.
* No secret masking in replies, hidden-character cleaning, system-prompt scrubbing or guard
  alerts: Mastra ships these, Agno does not, and the founder chose guards both ship.
* No inherited safety test set, bill of materials, attested images, rollback CI job, UAT
  promotion or `ci init` command (cut in the suggestions review).
* No Console change.

## Open questions

None blocking. Older SDKs ignore the `guardrails:` key (agent frontmatter ignores unknown keys), so
rolling back to a version without guardrails turns them off silently. `docs/rollback.md` says so.

## Definition of done

* Focused tests: `--required` selection and every exit-2 case, provenance fields round-trip,
  guardrail config parsing (including YAML `off`), injection block and warn with a stub
  classifier, personal-data masking in and out (including a value split across chunks),
  `on_error`, nested-call skip, `guardrails=False` builds, the audit row, and eval result guard
  records.
* E2E on a copy of `HubzoidTestHub/test-hub` with the real claude-local model: an injection
  message is blocked in web chat and over the API, an ordinary message is answered, an email and
  card number are masked in a reply, the Activity page shows the decisions,
  `hubzoid eval run --required` passes, fails and exits 2 where it should, and doctor shows `ok`
  after a passing run.
* CHANGELOG under 2.0.9. Full test suite in CI on the PR.

## Changes after the Codex plan review (20 Nov)

One Codex review (gpt-6.1-sol, high). Findings checked against the code and applied:

1. **A blocked message never comes back.** Chat history for the model (`chat/history.build_prompt`)
   leaves out a user message whose reply is the blocked sentence, and that reply. Multi-turn evals
   do the same. Personal data is masked across the whole prompt the guard receives, history
   included, so a masked value never reappears in a later turn or in Claude's full-history retry.
2. **The API checks the whole conversation it is sent.** The client supplies every message, so all
   of it is new input. Only stored chat history (the chat engine) narrows the check to the new
   message.
3. **Regenerate refinements are checked.** The chat engine's new input is the new user message plus
   any refinement text.
4. **The check runs on a worker thread** (`asyncio.to_thread`, which keeps the caller's context).
   A cancelled turn stops waiting at once. The check itself ends within its 60 second timeout.
5. **Images and files are not checked.** The guard reads text. An image goes to the model as it is.
   Said plainly in the docs.
6. **Custom patterns run on the `regex` engine with a time limit per message** (already installed
   with LiteLLM, now listed as a dependency), so a slow pattern cannot stall a reply. A pattern that
   runs out of time counts as a check failure (`on_error`). In a streamed reply a match longer than
   256 characters may be split, which the docs say.
7. **Reply masking covers what the person reads:** answer text, reasoning text and notices (such as
   the download footer). Tool calls and their results are shown as they ran.
8. **Workflows are not checked, including channel messages routed to a workflow and email intake.**
   The docs say so. Only messages that reach the agent as chat (web, channels with an agent, API,
   MCP, embed) are checked.
9. **Order is kept:** held-back text is released before the next tool or status event, and the
   wrapped stream is closed on cancel or early exit.
10. **Activity page shows decisions correctly without a UI change:** a block is recorded as `deny`,
    a warn or redact as `allow` with the reason starting `warn:` or `redact:`.
11. **Titles:** the conversation-title call (tool-free) gets the masked text when personal data
    masking is on. It is a separate call and not checked for injection, which the docs say.
12. **Multi-turn evals** name each turn's new message, as chat does.
13. **`--required` with `--model` exits 2** (doctor would not count a run on another model), and so
    does a hub too large to hash (doctor could never show it `ok`).
14. **A malformed or non-finite score is a check failure** (`on_error`). The check's usage row
    carries the person, surface and chat.
