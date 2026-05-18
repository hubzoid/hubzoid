# AGENTS.md. for AI editors working on the hubzoid platform

This file guides AI coding tools (Claude Code, Cursor, Codex, Copilot, Gemini
CLI, etc.) editing the hubzoid platform source. The per-hub `demo-hub/AGENTS.md`
is a different thing. that's the runtime prompt for the hub's main agent.

## Project layout

| Path | Purpose |
|---|---|
| `hubzoid/` | The installable Python package. |
| `hubzoid/loaders/` | Walks a hub directory and loads markdown into objects. |
| `hubzoid/tools/` | Pre-shipped tool factories. Each module exposes `make(ctx) -> list[FunctionTool]`. |
| `hubzoid/templates/starter/` | Bundled template used by `hubzoid init`. |
| `demo-hub/` | The canonical starter hub at the repo root (mirrors the template). |
| `server.py` | FastAPI bridge serving `/v1/chat/completions` + `/v1/models`. |
| `cli.py` | Typer-based CLI. |
| `factory.py` | `build_agent(hub_dir)`. composes everything. |

## Editing rules

- The hub structure is the contract. Adding required files or fields is a
  breaking change for every existing hub. Avoid.
- Folder names are case- and plural-insensitive (see `hubzoid/_fs.py`). Any new
  bucket must register an alias there.
- Frontmatter is pydantic-validated. Add new optional fields freely; don't
  add new required ones without a major-version bump.
- Pre-shipped tools must scope writes to `<hub>/output/<session>/`. Reads
  may go anywhere under the hub directory. No filesystem access outside the
  hub root.
- Open WebUI is a required dep but is invoked as a subprocess; the package
  must not `import open_webui` at module load (keeps cold-start fast and lets
  the bridge run headless via `--no-ui`).
- **Runtime neutrality (load-bearing rule).** Hubzoid supports multiple
  execution backends (today: OpenAI Agents SDK and Claude Agent SDK). A
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
      `factory.py`, `factory_claude.py`, `runtime.py`, `server.py`,
      and `cli.py`. Adding it elsewhere is a review-blocking smell.
    - Before adding a new tool or loader, ask: "would this behave the same
      under either backend?" If no, redesign or push the divergence into
      the runtime adapter.

## Testing rules

- Every loader + tool factory needs a unit test in `tests/`.
- Real-LLM tests live in `tests/e2e/` and are marked `e2e`. They must
  auto-skip when no provider key is set.
- Run the full suite before opening a PR: `pytest`.

## Style

- Python 3.11+. Effective upper bound is whatever open-webui currently supports (today: 3.12). Use `from __future__ import annotations`.
- Stdlib + pinned deps only (see `pyproject.toml`).
- Logging via `logging.getLogger(__name__)`; level is set by the CLI.
- No print statements in library code (CLI is allowed to use `rich.console`).
