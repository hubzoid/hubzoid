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

`tools:` is a whitelist over the combined registry of pre-shipped +
`tools_local/` tools. It scopes a **delegate** (see below); for a plain
inline sub-agent it is ignored (the main agent owns all tools).

When `description` reads like a "when" sentence, the main agent uses it to
decide when to reach for this sub-agent. Make it specific to avoid wrong
routing.

### Sub-agent models (delegation)

A sub-agent is loaded inline by the main agent (as a skill) by default. If
its frontmatter declares a `model:` that **differs** from the hub's model
*on the same engine*, it instead becomes a **delegate**: the main agent
calls it as a subagent running on that model, gets its answer, and continues
— the main agent stays in control the whole time.

- claude-local hub + `model: claude-local/opus` sub-agent → delegate on Opus.
- OpenAI/LiteLLM hub + a different LiteLLM `model:` → delegate on that model.
- Same model as the hub, a different *engine* (e.g. `gpt-4o` inside a
  claude-local hub), or no `model:` → stays an inline skill.

A delegate's `tools:` whitelist scopes which hub tools it may use. If the
delegate's model needs a provider key that is missing, it falls back to an
inline skill so the hub still boots.

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

## Evals

One markdown file per behavioural check, under `evals/`. The filename is the
case name; everything in frontmatter is optional.

```markdown
---
contains: ["14 days"]
expect_tools: [read_knowledge]
forbid_tools: [http_get]
# schedule: "0 6 * * 1"     # run itself weekly inside `hubzoid run`
---
## Prompt
What is the refund window for a cancelled program?

## Criteria
States 14 days. Does not invent an exception process.
```

A `## Criteria` section is what turns the model judge on — there is no
separate flag. The judge grades against this hub's own `AGENTS.md`, so rules
you wrote there are enforced without restating them here.

```
hubzoid eval run <hub>              # exit code is the CI gate
hubzoid eval run <hub> --no-judge   # free checks only, no cost
```

Full reference: [evals.md](evals.md).

## `.env`

```bash
OPENROUTER_API_KEY=sk-or-v1-...
MODEL=openrouter/anthropic/claude-haiku-4.5   # unset => defaults to claude-local (Claude Agent SDK, Sonnet)

# Optional knobs:
BRIDGE_API_KEYS=dev               # first key is what the UI sends
MODEL_LABEL=                      # what /v1/models shows; blank = derive from AGENTS.md name
WEBUI_NAME=                       # tab/top-bar title; blank = derive from AGENTS.md name, else "Hubzoid"
PORT=3080                         # UI port
BRIDGE_PORT=8000                  # bridge port
HTTP_ALLOWLIST=                   # comma-separated hostnames for http_get
HUB_LOG_LEVEL=info                # info | debug | warning
```
