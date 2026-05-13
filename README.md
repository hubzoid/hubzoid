# hubzoid

> Drop a folder of markdown files, get a chat agent with a polished web UI.

hubzoid reads `AGENTS.md`, `agents/`, `skills/`, and `knowledge/` from a folder
and turns it into a running AI agent, backed by the [OpenAI Agents
SDK](https://openai.github.io/openai-agents-python/), served over an
OpenAI compatible HTTP API, and chattable through a bundled
[Open WebUI](https://openwebui.com) front end. You write the markdown.
hubzoid handles the runtime, the API, the UI, the streaming, and the
sub agent routing. Provider agnostic via [LiteLLM](https://docs.litellm.ai).
OpenRouter, OpenAI, and Anthropic are supported out of the box.

Two equivalent ways to start. Both give you the same `my-hub/` folder with
the same starter content. Pick whichever fits your workflow.

```
┌─────────────────────────────┐
│  Open WebUI                 │  http://localhost:3080
│  (web chat, white label)    │
└──────────────┬──────────────┘
               │ OpenAI compatible HTTP
┌──────────────┴──────────────┐
│  FastAPI bridge             │  /v1/chat/completions  /v1/models
└──────────────┬──────────────┘
               │ in process
┌──────────────┴──────────────┐
│  OpenAI Agents SDK          │  sub agents, tools, handoffs, MCP
└──────────────┬──────────────┘
               │ LiteLLM
┌──────────────┴──────────────┐
│  Your model                 │  OpenRouter, OpenAI, Anthropic, others
└─────────────────────────────┘
```

## Quickstart

Requires Python 3.10 or newer.

### Clone

```bash
git clone https://github.com/hubzoid/hubzoid.git
cd hubzoid
pip install -r requirements.txt
```

The repo ships with `my-hub/` at the repo root. Set it up before running.
Stay at the repo root for these commands.

```bash
cp my-hub/.env.example my-hub/.env
```

Now edit two files in `my-hub/`.

1. `my-hub/.env`. Paste a key from OpenRouter, OpenAI, or Anthropic. Pick a
   model. The file shows you how.
2. `my-hub/AGENTS.md`. The body is your system prompt. Edit it or keep the
   starter.

See [Editing your hub](#editing-your-hub) below for everything else you can
change.

Run from the repo root:

```bash
python -m hubzoid run my-hub
```

(If you `cd` into `my-hub/` first, run `python -m hubzoid run .` instead.)

Open http://localhost:3080.

### Pip

```bash
pip install 'hubzoid[ui]'
hubzoid init my-hub
```

`hubzoid init` writes the same starter files anywhere on disk. Then:

```bash
cp my-hub/.env.example my-hub/.env
# Edit my-hub/.env (key + model) and my-hub/AGENTS.md (your prompt).
hubzoid run my-hub
```

Same starter content, same result.

> **Note.** The `[ui]` extra bundles Open WebUI (about 500 MB on first boot
> for its embedding model). If you only need the HTTP API, install bare
> `hubzoid` and start with `hubzoid run --no-ui my-hub`.

## Editing your hub

Your hub is one folder. Open `my-hub/`. Six things to know.

1. **Add your API key.** Copy `.env.example` to `.env` and paste a key from
   OpenRouter, OpenAI, or Anthropic. Pick a model (the file shows you how).
2. **Write the main agent.** Open `AGENTS.md`. The body is the system prompt.
   The YAML frontmatter sets `name`, `description`, and an optional `model`.
3. **(Optional) Sub agents.** One folder per sub agent under `agents/`. Each
   has its own `AGENTS.md`. Frontmatter `tools: [...]` whitelists which
   tools the sub agent may call.
4. **(Optional) Skills.** One folder per playbook under `skills/`, each with
   a `SKILL.md`. The main agent loads them on demand via `load_skill(name)`.
5. **(Optional) Knowledge.** One markdown file per topic under `knowledge/`.
   Reached via `read_knowledge(name)`.
6. **(Optional) Tools and connectors.** Drop Python files with
   `@function_tool` in `tools_local/`. Edit `connectors/.mcp.json` to plug
   in [MCP](https://modelcontextprotocol.io) servers.

Folder names are case and plural flexible. `skills/`, `Skills/`, and
`skill/` all work. Same for `agents/`, `knowledge/`, `tools_local/`,
`connectors/`.

Restart with the same command. Your changes are picked up on the next start.

## A minimal AGENTS.md

```markdown
---
name: my-bot
description: A helpful, concise assistant.
model: openrouter/anthropic/claude-haiku-4.5
---

You are a helpful assistant. Be concise. Cite sources when you use the web.
```

That is the whole hub if you do not need sub agents, skills, or knowledge.

## CLI

```
hubzoid init [PATH]              Scaffold a hub from the bundled starter template.
hubzoid run [PATH]               Start the FastAPI bridge plus Open WebUI for a hub.
  --port INT                       Open WebUI port (default 3080).
  --bridge-port INT                FastAPI bridge port (default 8000).
  --no-ui                          Bridge only, no Open WebUI.
hubzoid doctor [PATH]            Validate hub config and report issues.
hubzoid test [PATH]              Send one prompt to the agent and print the response.
hubzoid version
hubzoid --help
```

PATH defaults to `.` everywhere.

## Pre shipped tools

Every hub gets these tools for free.

| Tool | What it does |
|---|---|
| `read_file(path)` | Read a file under the hub directory. |
| `list_files(glob)` | List files matching a glob. |
| `write_artifact(filename, content)` | Write a file under `output/<session>/`. |
| `list_skills()` | Menu of skills in the hub. |
| `load_skill(name)` | Read a skill's full body on demand. |
| `list_knowledge()` | Menu of knowledge documents. |
| `read_knowledge(name)` | Read a knowledge document's full body. |
| `remember(fact, scope)` | Save a fact to durable memory. |
| `recall(query, scope)` | Look up saved facts. |
| `forget(id, scope)` | Delete a saved fact. |
| `render_jinja(template, context_json)` | Render a Jinja2 template. |
| `http_get(url)` | Fetch a URL (honors `HTTP_ALLOWLIST`). |
| `web_search(query)` | DuckDuckGo search. No API key. |

Custom tools dropped into `tools_local/*.py` are auto discovered.

## Adding MCP connectors

Edit `connectors/.mcp.json`.

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["@modelcontextprotocol/server-filesystem", "./workspace"]
    },
    "github": {
      "command": "npx",
      "args": ["@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GH_TOKEN}"}
    }
  }
}
```

`${VAR}` references in any string field resolve against the environment at
boot.

## Providers

Pick one stanza in `.env`. See [docs/providers.md](docs/providers.md) for
more.

```bash
# OpenRouter (one key, many models)
OPENROUTER_API_KEY=sk-or-v1-...
MODEL=openrouter/anthropic/claude-haiku-4.5

# OR OpenAI
OPENAI_API_KEY=sk-...
MODEL=openai/gpt-4o-mini

# OR Anthropic
ANTHROPIC_API_KEY=sk-ant-...
MODEL=anthropic/claude-haiku-4-5
```

The `MODEL` string tells LiteLLM which provider to call. The matching key
must be set.

## What you get out of the box

* **Web chat** (Open WebUI). Multi user, file uploads, conversation history,
  code rendering. White label via `WEBUI_NAME` env.
* **HTTP API** at `/v1/chat/completions`. Any OpenAI client just works.
* **Sub agents and handoffs.** Markdown definition, OpenAI Agents SDK
  runtime.
* **Tool calls.** File ops, knowledge, skills, HTTP, web search, render,
  memory are pre shipped. Bring your own with `tools_local/`.
* **Skills.** Progressive playbooks via `load_skill`.
* **MCP.** Any MCP server works as a tool source.
* **Persistent memory.** Per session filesystem memory.
* **Provider agnostic.** Anything LiteLLM speaks.

## What you do not write

* No runtime code.
* No FastAPI wiring.
* No chat UI work.
* No prompt engineering scaffolding.
* No tool registration boilerplate.

## Open standards

| Spec | Used at |
|---|---|
| [AGENTS.md](https://agents.md) | `<hub>/AGENTS.md`, `<hub>/agents/<n>/AGENTS.md` |
| SKILL.md | `<hub>/skills/<n>/SKILL.md` |
| [MCP](https://modelcontextprotocol.io) | `<hub>/connectors/.mcp.json` |

Hubs are portable across any tool that adopts these specs (Claude Code,
Cursor, Codex, Copilot, Gemini CLI, VS Code).

## Status and roadmap

* **v0.1.** This release. CLI, bridge, Open WebUI, AGENTS.md, SKILL.md, MCP
  loaders, OpenRouter, OpenAI, Anthropic.
* **v1.1.** Docker bundle, Slack, Telegram, email digest adapters.
* **v1.2.** Mem0 and Zep memory backends.
* **v1.3.** Hosted multi tenancy.

## Non goals

* Voice and realtime.
* Visual agent builder. Markdown is the IDE.

## License

MIT.
