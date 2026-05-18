# Authoring a hub

A hub is one folder. The shape:

```
demo-hub/
├── .env                          keys + model id (gitignored)
├── AGENTS.md                     main agent: prompt + frontmatter
├── agents/<name>/AGENTS.md       sub-agent (handoff target)
├── skills/<name>/SKILL.md        playbook loaded on demand
├── knowledge/<topic>.md          long-form domain content
├── connectors/.mcp.json          optional MCP servers
├── tools_local/*.py              optional custom Python tools
└── output/                       runtime-managed
```

Folder names are flexible. `Skills/`, `skill/`, `Skill/` all work.

## `AGENTS.md`. required

The hub's "main agent". A plain markdown file. Frontmatter is optional.

The simplest form is just markdown:

```markdown
You are a helpful, concise assistant. Reply in under 4 sentences.
```

With no frontmatter, `name` defaults to the hub folder name and
`description` is derived from the first non heading line of the body.

If you want to control them explicitly, add a YAML frontmatter block:

```markdown
---
name: my-bot                                 # optional; shown in /v1/models
description: A helpful, concise assistant.   # optional; used as handoff trigger for sub agents
model: openrouter/anthropic/claude-haiku-4.5 # optional; overrides .env MODEL
---

Body is the system prompt. Anything here goes verbatim into the agent's
`instructions`.
```

## Sub-agents

Drop folders under `agents/`. Each folder needs an `AGENTS.md`:

```markdown
---
name: researcher
description: When the user wants a researched brief.   # used as handoff trigger
tools: [web_search, http_get, read_knowledge, write_artifact]
model: openrouter/anthropic/claude-haiku-4.5           # optional
---

You are the researcher sub-agent. ...
```

`tools:` is a whitelist. The agent refuses to start if a name isn't in the
combined registry of pre-shipped + `tools_local/` tools.

When `description` reads like a "when" sentence, the main agent uses it as
the handoff trigger condition. Make it specific to avoid wrong routing.

## Skills

Skills are playbooks. The main agent sees a `load_skill` tool whose menu is
the list of skill names + descriptions. It loads the body only when needed.

```markdown
---
name: summarize
description: Three-bullet summary of a document.
---

When asked to summarize:
1. ...
```

## Knowledge

Long-form content. The main agent sees `list_knowledge` + `read_knowledge`.

```markdown
---
name: jexl_expressions
description: JEXL syntax reference.
keywords: [jexl, validation, expression]
---

# JEXL Expressions
...
```

If frontmatter is missing, the filename stem becomes the name and a
generic description is used.

## Custom tools

```python
# tools_local/my_tool.py
from agents import function_tool

@function_tool
def lookup_order(order_id: str) -> dict:
    """Look up an order by ID."""
    return {"id": order_id, "status": "shipped"}
```

Files starting with `_` are ignored. Reference the tool by its function name
in a sub-agent's `tools:` list.

## MCP connectors

`connectors/.mcp.json`:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["@modelcontextprotocol/server-filesystem", "./workspace"]
    }
  }
}
```

`${VAR}` references are resolved against the environment at boot.

## `.env`

```bash
OPENROUTER_API_KEY=sk-or-v1-...
MODEL=openrouter/anthropic/claude-haiku-4.5

# Optional knobs:
BRIDGE_API_KEYS=dev               # first key is what Open WebUI sends
MODEL_LABEL=                      # what /v1/models shows; blank = derive from AGENTS.md name
WEBUI_NAME=                       # Open WebUI title; blank = default
PORT=3080                         # UI port
BRIDGE_PORT=8000                  # bridge port
HTTP_ALLOWLIST=                   # comma-separated hostnames for http_get
HUB_LOG_LEVEL=info                # info | debug | warning
```
