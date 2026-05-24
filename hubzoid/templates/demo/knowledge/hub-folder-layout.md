---
name: hub-folder-layout
description: What every folder inside a Hubzoid hub means. The anatomy of a hub.
keywords: [folder, layout, structure, anatomy, hub]
---

# Hub folder layout

A Hubzoid hub is one folder on disk. Here is what each entry does.

```
demo-hub/
├── AGENTS.md              # main agent. Required.
├── agents/                # sub-agents. Optional.
│   └── builder.md
├── skills/                # on-demand playbooks. Optional.
│   ├── explain-skills.md
│   ├── build-first-agent.md
│   ├── inspect-this-hub.md
│   └── find-the-docs.md
├── knowledge/             # long-form domain content. Optional.
│   ├── what-is-hubzoid.md
│   ├── three-agent-types.md
│   ├── agents-md-format.md
│   ├── hub-folder-layout.md
│   ├── mcp-and-connectors.md
│   └── welcome.md
├── tools_local/           # custom Python tools. Optional.
│   └── word_count.py
├── connectors/            # MCP server config. Optional.
│   └── .mcp.json
├── branding/              # logo / favicon / splash. Optional.
├── output/                # session artifacts the agent writes. Auto-created.
├── .env                   # MODEL + keys + UI knobs. Git-ignored.
└── .gitignore
```

## Folder semantics

| Folder | What lives there | Reached by |
|---|---|---|
| `agents/` | Sub-agents. One `<name>.md` per sub-agent, or a `<name>/AGENTS.md` folder when it ships supporting files. | Handoff from main agent. |
| `skills/` | Playbooks. One `<name>.md` per skill, or a `<name>/SKILL.md` folder when it ships supporting files. | `load_skill(name)` at run time. |
| `knowledge/` | Reference content. One markdown file per topic. | `read_knowledge(name)` at run time. |
| `tools_local/` | Python tools. Any `@function_tool` callable. | Auto-discovered at boot. |
| `connectors/` | `.mcp.json` configuring MCP servers. | Loaded at boot. |
| `branding/` | Logo, favicon, splash. Used by `hubzoid run`. | Applied to Open WebUI. |
| `output/` | Files the agent writes via `write_artifact`. | Per-session subfolders. |

## Naming flexibility

Folder names are case- and plural-flexible. `skills/`, `Skills/`, and
`skill/` all work. Same for `agents/`, `knowledge/`, `tools_local/`,
`connectors/`. The loader does case-insensitive plural matching.

## What is NOT in the hub

- No runtime code. The Python package handles that.
- No prompt scaffolding. Just the system prompts you write.
- No tool registration boilerplate. Drop a `@function_tool` and you are done.

## Restart to reload

Hubzoid loads the hub on process start. Edit any file, then restart with
the same command. Changes are picked up on the next start.
