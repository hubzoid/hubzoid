# AGENTS.md. for AI editors working on the hubzoid platform

## Product direction · 2026-10-07

Read [the product brief](docs/PRODUCT_CONTEXT.md) before product, architecture,
or user-facing work. Its revision is HZ-DIRECTION-2026-10-07-v5.

Hubzoid is the single open-source product focus: shared context and capabilities
used through workflows, chat, and personal assistants through MCP. The primary
audience is AI-capable builders bringing useful personal agents to their teams.
Useful independent usage and sustained use guide growth. Stars, issues and PRs
are optional. A distinct Build with us services path serves business and
technology leaders. GitZoid is a sample application direction.

This brief supersedes older services-led product positioning. It is direction,
not a release feature list. After 1.1.1, prioritize stability, testing and friction
in first and repeat use. Older implementation workstreams are a reference
inventory, not an automatic feature queue. Native chat is the current default.
Easy customer hosting is a framework requirement. Provider-specific implementation,
memory and additional onboarding variants still need scoped decisions.
This context update authorizes no migration.
The engineering, compatibility, licensing, and validation rules below still apply.

This file guides AI coding tools (Claude Code, Cursor, Codex, Copilot, Gemini
CLI, etc.) editing the hubzoid platform source. The per-hub `demo-hub/AGENTS.md`
is a different thing. that's the runtime prompt for the hub's main agent.

## Adoption and stabilization

Keep the core tagline **One brain for your internal agents.** Add the value
message **Your knowledge. Your way of working. Useful across your assistants and your team.**
Help builders make agents for their own work and company. The first-use
milestone is a useful result from their own material and rules, followed by
an explicitly saved correction that changes the next result. Keep a runnable
sample with a clear personalization step.

Use Claude Code or Codex as the primary authoring path for this audience,
alongside human-readable instructions. Authoring, MCP access and runtime
selection are separate. Only explicit Hub content is shared. Do not imply
that setup conversation becomes shared knowledge or automatic memory.
Prioritize the path from personal usefulness to repeat work, shared context
and a teammate with appropriate access when removing adoption friction.

Every promoted setup path needs release-bound prerequisites, a useful sample
result, troubleshooting, honest costs and limits, and human-readable plus
agent-readable instructions. Start with one reliable path and add variants
progressively. Preserve a usable path for AI-capable non-engineers.

Connecting an assistant over MCP, adapting an existing agent and hosting an
existing runtime are different operations. Do not claim one-prompt portability
or subscription-based team hosting without evidence for that combination.

Reproduce historical findings before calling them current bugs or completed
fixes. For release confidence, inspect heavy-test exclusions and timeout skips.
A skipped or timed-out critical check does not count as a pass. Keep targeted
regression checks with fixes and record real-provider validation gaps.

## Required roadmap scope

Required outcomes include stability, building agents from personal/team context,
easy customer hosting, Build and Use modes, human-in-the-loop workflows and
actions, RAG/retrieval, controlled memory, and multi-agent communication.
Their inclusion is approved. Implementation designs and release allocations
remain to be scoped through proposals. Begin with bounded useful examples and
existing components, retaining runtime neutrality and Hub compatibility.
Proposed Git-backed Build changes and approval of business actions are distinct concerns.
Do not treat planned capabilities as already shipped.

## Customer-hosting requirement

The framework must make customer-controlled hosting straightforward. Railway
and Maritime are the requested provider examples, with support to be implemented
and validated. Prefer provider-native deployment configuration or templates and
existing packaging over a custom hosting layer when they meet the need.

Each promoted path needs release-bound human and agent instructions covering
model authentication, secrets, persistent Hub and operational data, public URLs,
sign-in/MCP callbacks, scoped access, costs, troubleshooting, backup/restore and
upgrades. Verify a useful task, teammate access, restart persistence, workflow
and scheduler behaviour, and any provider sleep/wake limits. Record the tested
release, provider configuration, runtime and limits. A working local assistant
login does not establish unattended hosted or shared subscription use.

This is a framework requirement, separate from a public demo or optional
Hubzoid-managed hosting. Use the proposal workflow for non-trivial implementation.
Do not claim provider support from this instruction alone or provision paid
resources without the actual authorized scope. Preserve the name Maritime.

## Prime directive: keep it thin

**Minimize total ownership cost for the experience we need. Prefer existing
solutions; own the gaps that demonstrably matter.**

This is what "keep it thin" means. Before adding or replacing code, ask:

1. **What observed customer or operating need does this address?**
2. **Can an existing component or integrated solution provide the required
   experience at lower total ownership cost?**
3. **If we own the gap, does its demonstrated value justify ongoing maintenance?**

Count implementation, integration, deployment, upgrades, support, and migration
costs, not just lines of custom code. Prefer existing solutions when they meet
the need. Own custom code, including UI, when the gap demonstrably matters.
Replace owned code when an existing solution lowers total cost while preserving
the required experience, accounting for transition costs and practical
portability. Neither custom UI nor a larger dependency is automatically
disallowed; neither is justified by feature breadth alone.

Hubzoid is **Apache-2.0, all of it** (see `LICENSING.md`) — there is no enterprise
edition, license key, or paid tier in this repo; don't add feature gating.

Non-trivial changes come in as text in `proposals/`, not as large code PRs. See
`CONTRIBUTING.md` and the `skills/` (`propose-change`, `implement-proposal`).

## Project layout

| Path | Purpose |
|---|---|
| `hubzoid/` | The installable Python package. |
| `hubzoid/loaders/` | Walks a hub directory and loads markdown into objects. |
| `hubzoid/tools/` | Pre-shipped tool factories. Each module exposes `make(ctx) -> list[FunctionTool]`. |
| `hubzoid/templates/operations/` | Default `hubzoid init` template. A runnable operations assistant with a stock-check example. |
| `hubzoid/templates/minimal/` | Small runnable hub, selected with `--template minimal`. |
| `hubzoid/templates/demo/` | Full guided tour. Selected via `hubzoid init <name> --template demo`. |
| `hubzoid/templates/watchtower/` | Workflow-first sample (scheduled check, structured `call_llm`, controlled failure). `--template watchtower`. |
| `hubzoid/templates/gitzoid/` | PR review, security and engineering-report sample, with fictional data and optional GitHub setup. `--template gitzoid`. |
| `demo-hub/` | The canonical demo hub at the repo root (mirrors `templates/demo/`). |
| `server.py` | FastAPI bridge serving `/v1/chat/completions` + `/v1/models` + `/artifacts`. |
| `edge.py` | Public reverse proxy: the native chat app and Console by default; Open WebUI and bridge routes in Open WebUI mode. |
| `gateway.py` | Plans one deployment over many hub bridges (`hubzoid gateway`), for either chat UI. |
| `scheduling.py` | Scheduled-task declarations: cron parsing, `<hub>/schedule/*.md` loader, fire-state, run lock. |
| `scheduler.py` | In-process tick loop (started by the bridge lifespan) that fires due tasks while the hub is idle. |
| `schedule_runner.py` | Executes one task: fresh-context rounds via the hub Runtime until `STATUS: DONE`, then scoped commit/push. |
| `tools/schedule_tools.py` | `run_git` + `write_hub_file` — internal tools injected only into scheduled runs, never chat. |
| `evals/` | Hub-owned behavioural checks (`<hub>/evals/*.md`): format, free assertions, runner, judge, report, optional Langfuse push. |
| `evals/schedule.py` | Second task source for `scheduler.py` — a due eval case runs the deterministic runner, not the agent harness. |
| `cli.py` | Typer-based CLI. |
| `factory.py`, `factory_claude.py`, `factory_codex.py`, `runtime.py` | Runtime adapters; SDK construction belongs here. |
| `chat/`, `auth/`, `webapp_gateway.py` | Native conversations, sign-in and the shared gateway app. |
| `workflows/` | Durable execution, observation, identity and controls over DBOS. |

## Editing rules

- DBOS is pinned (`dbos==3.1.0`). Workflow ownership loss depends on DBOS
  recording only `Exception` outcomes: `ownership.OwnershipLost` is a
  `BaseException`, so a run that meets an ownership guard stays recoverable
  instead of failing. Before changing the pin, read DBOS's step and workflow
  outcome handling again and run `pytest -m ""
  tests/test_postgres_acceptance.py::test_postgres_owner_loss_stops_claiming_and_keeps_work_recoverable`
  (with the rest of `tests/test_workflow*.py`). It must pass unchanged. It is
  marked `slow`, so a plain `pytest` skips it.
- The hub structure is the contract. Adding required files or fields is a
  breaking change for every existing hub. Avoid.
- Folder names are case- and plural-insensitive (see `hubzoid/_fs.py`). Any new
  bucket must register an alias there.
- Frontmatter is pydantic-validated. Add new optional fields freely; don't
  add new required ones without a major-version bump.
- Pre-shipped tools must scope writes to `<hub>/output/<session>/`. Reads
  may go anywhere under the hub directory. No filesystem access outside the
  hub root.
- Open WebUI (Open WebUI mode) is an optional extra invoked as a subprocess; the package
  must not `import open_webui` at module load (keeps cold-start fast and lets
  the bridge run headless via `--no-ui`).
- **Runtime neutrality (load-bearing rule).** Hubzoid supports multiple
  execution backends (today: OpenAI Agents SDK Claude Agent SDK and Codex app-server). A
  single hub folder must produce identical manual-testing surface across
  backends — same tool names, same input schemas, same outputs, same skills,
  same knowledge. To keep that invariant:
    - `hubzoid/loaders/*` (except `tools_local.py`) must not import from any
      runtime SDK. They return plain dataclasses / pydantic models.
    - `hubzoid/tools/*` and `loaders/tools_local.py` may use the OpenAI
      Agents SDK `FunctionTool` shape (name, description, params_json_schema,
      on_invoke_tool) as the canonical tool representation. Other runtimes
      consume those four fields via a thin adapter
      (`hubzoid/factory_claude.py: to_claude_tool`).
    - Runtime-specific construction (`Agent(...)`, `ClaudeAgentOptions(...)`,
      `Runner.run_streamed(...)`, `query(...)`) lives ONLY in
      `factory.py`, `factory_claude.py`, `factory_codex.py`, `runtime.py`, `server.py`,
      and `cli.py`. Adding it elsewhere is a review-blocking smell.
    - Before adding a new tool or loader, ask: "would this behave the same
      under either backend?" If no, redesign or push the divergence into
      the runtime adapter.

## Testing rules

- Every loader + tool factory needs a unit test in `tests/`.
- Real-LLM tests live in `tests/e2e/` and are marked `e2e`. They must
  auto-skip when no provider key is set.
- During implementation, run focused tests for the changed behavior. Do not
  repeatedly run the full suite after small edits. Rerun checks only when a
  relevant change, failure, or unresolved risk justifies it.
- Keep tests lean: cover distinct behavior, compatibility, and failure risks.
  Prefer extending existing tests; avoid redundant cases and tests that merely
  mirror the implementation. Reversible documentation or cosmetic edits need
  no new tests.
- Run one broader regression check before the final release/PR handoff, after
  focused checks pass. Choose its scope for the change; a cross-cutting release
  warrants the full suite (`pytest -m ""`) once, not on every implementation step.
- Commands: `pytest tests/test_x.py -k name` (focused) · `pytest` (fast default,
  ~80 s; skips `slow` and `e2e*`) · `pytest -m ""` (everything, as CI runs it).
  Mark any new test that starts DBOS, Postgres or a subprocess `@pytest.mark.slow`.
- The eval runner (`hubzoid/evals/`) must stay model-free in its logic: the
  judge's model call and the runner's judge are both injected seams, so the
  whole suite path is testable with no model and no network. Keep it that way.

## Style

- Python 3.11+. Effective upper bound is whatever open-webui currently supports (today: 3.12). Use `from __future__ import annotations`.
- Stdlib + pinned deps only (see `pyproject.toml`).
- Logging via `logging.getLogger(__name__)`; level is set by the CLI.
- No print statements in library code (CLI is allowed to use `rich.console`).
