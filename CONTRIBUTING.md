# Contributing

Thanks for being here. Hubzoid is MIT and we're glad to have you.

Hubzoid is built by a small team that does not want to maintain a large surface.
So the contribution model is deliberately **light**: we'd rather have a clear
idea we can build well than a big PR we have to own forever. Three lanes.

## 1. An idea, a question, "would you take X?" → open a Discussion

[GitHub Discussions](https://github.com/hubzoid/hubzoid/discussions) is the front
door. Feature ideas, design questions, "is this a bug or am I holding it wrong" —
start here. This is the best way to help, and it costs you nothing.

## 2. Small and obvious → open a PR directly

Typo, doc fix, a clearly-scoped bug with a test. Just send it. Sign your commits
off (DCO):

```bash
git commit -s -m "fix: ..."
```

The `-s` adds a `Signed-off-by` line certifying you wrote the change and can
contribute it under the MIT license (see [DCO](https://developercertificate.org/)).
That's all we ask — no CLA.

## 3. A feature or anything non-trivial → propose it as text, not code

Don't open a large code PR unprompted; it's the one thing likely to be closed
unread. Instead write the *intent* as a short markdown proposal in
[`proposals/`](proposals/) and open a PR with just that file. We (or an agent)
discuss it, and once it's agreed we implement it.

This keeps the codebase thin and coherent, and it means non-developers can shape
Hubzoid too. The [`propose-change`](skills/propose-change.md) skill will walk you
through writing one; [`proposals/TEMPLATE.md`](proposals/TEMPLATE.md) is the form.

## Honest note

Hubzoid is the engine behind WaveAssist's services work, released under MIT so
you're never locked in. We steer direction, and we may decline good code simply
because we don't want to maintain it. That's not a knock on your work — it's the
doctrine that keeps the project small enough to trust.

## Dev setup

```bash
git clone https://github.com/hubzoid/hubzoid.git
cd hubzoid
pip install -e '.[dev]'
pytest
```

## Running tests

```bash
pytest                       # unit + integration (no LLM calls)
pytest -m e2e_llm            # also run real-LLM end-to-end (uses MODEL=claude-local subscription credit)
pytest -m e2e_ui             # Playwright UI tests against a fixture hub
```

The e2e tests boot the FastAPI bridge against `demo-hub/` and hit a real
provider. They are skipped automatically if no provider key is set.

## Code conventions

- Python 3.11+. Upper bound is whatever open-webui supports today.
- Keep the public API small. The blast radius of a breaking change in
  `factory.build_agent` or the CLI is large.
- Loaders go in `hubzoid/loaders/`, tools in `hubzoid/tools/`.
- Tools that need hub state take it via a context object in `make(ctx)`.
- Tests live in `tests/`. End-to-end tests in `tests/e2e/`.
- Document any new env var in `hubzoid/settings.py`.
- Read [`AGENTS.md`](AGENTS.md) first — especially the "keep it thin" rule.

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

## License

By contributing, you agree your contribution is licensed under the MIT
License (see `LICENSE`), and you certify the DCO sign-off above.
