# skills/

Repo-level skills for a coding agent working on Hubzoid. Plain markdown, so any
tool (Claude Code, Cursor, Codex, ...) can read them.

- **[`propose-change.md`](propose-change.md)** — turn an idea into a short
  `proposals/` document and open the PR.
- **[`implement-proposal.md`](implement-proposal.md)** — turn an agreed proposal
  into a plan, get it approved, then build and test it.

These are **not** Hubzoid runtime skills. A hub's own skills live in
`<hub>/skills/` and load at runtime via `load_skill()`. Nothing here is ever
loaded by a running hub. See [`AGENTS.md`](../AGENTS.md) for repo doctrine.
