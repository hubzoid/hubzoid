# skills/

Plain-markdown skills for coding agents. Any tool (Claude Code, Cursor, Codex,
...) can read them.

## For builders

- **[`builder/SKILL.md`](builder/SKILL.md)** (`build-hub`). Takes a
  builder from "here is my task, my material and my rules" to a running hub,
  with files and the CLI only: knowledge, a skill, a workflow, `hitl.yaml`,
  usage limits, MCP, evals, a teammate's access and `hubzoid doctor`.

- **[`embed/SKILL.md`](embed/SKILL.md)** (`hubzoid-embed`). Puts a hub's chat
  inside another app (ERP, dashboard, portal), signed in by that app, with
  assistant-ui or CopilotKit, and lets tools reach the app as the person.

In Claude Code, install it as a plugin:
`/plugin install hubzoid --marketplace hubzoid/hubzoid`, then `/hubzoid:build-hub`
([plugins/hubzoid](../plugins/hubzoid/README.md) carries a copy).
Or put it where your assistant finds project skills, or ask it to read the file:

```bash
mkdir -p .claude/skills/build-hub     # Codex: .agents/skills/build-hub
curl -fsSL https://raw.githubusercontent.com/hubzoid/hubzoid/main/skills/builder/SKILL.md \
  -o .claude/skills/build-hub/SKILL.md
```

Then, in Claude Code or Codex: "Use the build-hub skill. My task is ...,
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
