# Contributing

Thanks for considering a contribution.

## Dev setup

```bash
git clone https://github.com/hubzoid/hubzoid.git
cd hubzoid
pip install -e '.[ui,dev]'
pytest
```

## Running tests

```bash
pytest                       # unit + integration (no LLM calls)
pytest -m e2e                # also run real-LLM end-to-end (needs OPENROUTER_API_KEY)
```

The e2e tests boot the FastAPI bridge against `my-hub/` and hit a real
provider. They are skipped automatically if no provider key is set.

## Code conventions

- Python 3.10+.
- Keep the public API small. The blast radius of a breaking change in
  `factory.build_agent` or the CLI is large.
- Loaders go in `hubzoid/loaders/`, tools in `hubzoid/tools/`.
- Tools that need hub state take it via a context object in `make(ctx)`.
- Tests live in `tests/`. End-to-end tests in `tests/e2e/`.
- Document any new env var in `hubzoid/settings.py`.

## Runtime neutrality (important)

Hubzoid runs hub folders through more than one backend (today: OpenAI Agents
SDK, Claude Agent SDK via `MODEL=claude-local`). The same hub must produce
the same observable surface — tool names, schemas, skills, knowledge,
sub-agents — under any backend. Manual testing is done against one backend
at a time; divergence creates bug-report magnets.

To keep the invariant:

- `hubzoid/loaders/*` (except `tools_local.py`) must not import from a
  runtime SDK. They return plain data.
- `hubzoid/tools/*` and `loaders/tools_local.py` may use OpenAI Agents SDK
  `FunctionTool` as the canonical tool shape. Other runtimes adapt via the
  four exposed fields (`name`, `description`, `params_json_schema`,
  `on_invoke_tool`).
- Runtime construction (`Agent(...)`, `ClaudeAgentOptions(...)`, runners,
  `query(...)`) lives only in `factory.py`, `factory_claude.py`,
  `runtime.py`, `server.py`, `cli.py`.
- When adding a tool or loader, sanity-check both backends. New tools
  should not rely on OpenAI-SDK-specific behavior the Claude adapter
  can't replicate.

## What's in scope for v0.1

- Bug fixes.
- Doc improvements.
- Loader edge cases.
- Tests.

## What's out of scope (yet)

- New surfaces (Slack, Telegram). Tracked for v1.1.
- Alternative memory backends. Tracked for v1.2.
- Multi-tenancy. Tracked for v1.3.

## License

By contributing, you agree your contribution is licensed under the MIT
License (see `LICENSE`).
