# skills/

Plain-markdown skills for coding agents. Any tool (Claude Code, Cursor, Codex,
...) can read them.

## For builders

- **[`builder/SKILL.md`](builder/SKILL.md)** (`hubzoid-builder`). Takes a
  builder from "here is my task, my material and my rules" to a running hub,
  with files and the CLI only: knowledge, a skill, a workflow, `hitl.yaml`,
  usage limits, MCP, evals, a teammate's access and `hubzoid doctor`.

Put it where your assistant finds project skills, or ask it to read the file:

```bash
mkdir -p .claude/skills/hubzoid-builder     # Codex: .agents/skills/hubzoid-builder
curl -fsSL https://raw.githubusercontent.com/hubzoid/hubzoid/main/skills/builder/SKILL.md \
  -o .claude/skills/hubzoid-builder/SKILL.md
```

Then, in Claude Code or Codex: "Use the hubzoid-builder skill. My task is ...,
my material is in ./material, my rules are ...". The skill links to the pages
in [`docs/`](../docs/README.md) instead of repeating them.

## For contributors to Hubzoid

- **[`propose-change.md`](propose-change.md)**. Turn an idea into a short
  `proposals/` document and open the PR.
- **[`implement-proposal.md`](implement-proposal.md)**. Turn an agreed proposal
  into a plan, get it approved, then build and test it.

None of these are Hubzoid runtime skills. A hub's own skills live in
`<hub>/skills/` and load at runtime via `load_skill()`. Nothing here is ever
loaded by a running hub. See [`AGENTS.md`](../AGENTS.md) for repo doctrine.
