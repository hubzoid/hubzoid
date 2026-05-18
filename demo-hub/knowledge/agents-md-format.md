---
name: agents-md-format
description: The structure of an AGENTS.md file. Frontmatter fields, body conventions.
keywords: [agents.md, frontmatter, system prompt, format]
---

# The AGENTS.md format

`AGENTS.md` defines an agent. One file per agent. Format is the open
`agents.md` spec, which Claude Code, Cursor, Codex, Copilot, and Gemini
CLI all support. A Hubzoid hub is portable across those tools.

## Anatomy

```markdown
---
name: hubzoid-guide
description: One-line summary used when this agent is referenced from a parent.
model: claude-local
tools: [read_file, list_files]
---

System prompt body. This is what the model sees as its instructions.
Anything written here governs behavior.
```

## Frontmatter fields

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | The agent's identifier. Use kebab-case. |
| `description` | yes | One-line summary. Surfaces in handoff routing. |
| `model` | no | Override `MODEL` from `.env` for this agent only. |
| `tools` | no | Whitelist. Empty list means no tools. Omit to inherit the parent's full tool registry. |

## Body conventions

The body is your system prompt. Hubzoid does not impose structure. A few
patterns that work.

- **Voice section.** Tell the model how to talk. Concrete adjectives,
  example phrasings, forbidden words.
- **When to reach for what.** A small table: "if the user asks X, call Y".
  This is more reliable than telling the model "be smart about tool use".
- **Handoff rules.** When to invoke a sub-agent versus answer directly.
- **What you do not do.** Explicit negative space. Often more useful than
  positive instructions.

## Main agent vs sub-agents

A hub has one main agent. It lives at `<hub>/AGENTS.md`. Sub-agents live
at `<hub>/agents/<name>/AGENTS.md`. The main agent can hand off to any
sub-agent by name. Sub-agents do not nest in Hubzoid v0.2.

## See it in practice

The `AGENTS.md` you are reading is itself the system prompt for the
Hubzoid Guide. Open `demo-hub/AGENTS.md` to read it. The sub-agent is
at `demo-hub/agents/builder/AGENTS.md`.
