<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/hubzoid/hubzoid/main/assets/mark-dark.svg">
    <img alt="/hubzoid" src="https://raw.githubusercontent.com/hubzoid/hubzoid/main/assets/mark-light.svg" width="220">
  </picture>
</p>

<p align="center">
  <strong>An open-source framework for production AI agents.</strong><br>
  <sub>Defined in markdown. Deployed inside your perimeter. The substrate behind <a href="https://hubzoid.com">Hubzoid</a> customer deployments.</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/hubzoid/"><img src="https://img.shields.io/pypi/v/hubzoid?color=E5572A&label=pypi" alt="PyPI"></a>
  <a href="https://pypi.org/project/hubzoid/"><img src="https://img.shields.io/pypi/pyversions/hubzoid?color=0B0B0C" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-0B0B0C" alt="MIT License"></a>
  <a href="https://hubzoid.com"><img src="https://img.shields.io/badge/website-hubzoid.com-E5572A" alt="hubzoid.com"></a>
</p>

---

hubzoid reads `AGENTS.md`, `agents/`, `skills/`, and `knowledge/` from a folder
and turns it into a running AI agent. Backed by the [OpenAI Agents
SDK](https://openai.github.io/openai-agents-python/) or the [Claude Agent
SDK](https://code.claude.com/docs/en/agent-sdk/overview), served over an
OpenAI-compatible HTTP API, and chattable through a bundled
[Open WebUI](https://openwebui.com) front end.

You write the markdown. hubzoid handles the runtime, the API, the UI, the
streaming, and the sub-agent routing. Provider-agnostic via
[LiteLLM](https://docs.litellm.ai): OpenRouter, OpenAI, Anthropic, and local
Claude work out of the box.

## Quickstart

**3 steps if you have `claude` CLI installed and logged in, 4 otherwise.**

> Python 3.11 or 3.12 (Open WebUI does not yet support 3.13+). On recent macOS,
> the default `python3` is too new; create your venv with `python3.12 -m venv`
> explicitly. If pip tries to build `av` (PyAV) from source, run
> `brew install pkg-config ffmpeg` first.

```bash
pip install hubzoid
hubzoid init demo-hub                  # scaffolds a starter hub + agents-repo wrapper
*  edit demo-hub/.env                  # ← optional. skip if using claude-local.
hubzoid run demo-hub
```

Open <http://localhost:3080>. The scaffolded `demo-hub` is a working **Hubzoid
Guide** agent.

**\* Step 3 (the optional one).** Default `MODEL=claude-local` uses your
installed `claude` CLI subscription. If you already ran `claude login`,
skip this step and go straight to `hubzoid run`. Otherwise, open
`demo-hub/.env`, comment out the `MODEL=claude-local` line, and uncomment
one of the provider stanzas (OpenRouter, OpenAI, Anthropic) with your key
pasted in.

The two files you edit later as you customize:

1. `demo-hub/.env`: keys, model selection, UI knobs.
2. `demo-hub/AGENTS.md`: the system prompt body. YAML frontmatter sets
   `name`, `description`, and optional `model`.

## How it works

```
┌─────────────────────────────┐
│  Open WebUI                 │  http://localhost:3080
│  (web chat, white-label)    │
└──────────────┬──────────────┘
               │ OpenAI-compatible HTTP
┌──────────────┴──────────────┐
│  FastAPI bridge             │  /v1/chat/completions  /v1/models
└──────────────┬──────────────┘
               │ in-process
┌──────────────┴──────────────┐
│  Agent runtime              │  OpenAI Agents SDK  |  Claude Agent SDK
└──────────────┬──────────────┘
               │ LiteLLM (or claude CLI subprocess)
┌──────────────┴──────────────┐
│  Your model                 │  OpenRouter · OpenAI · Anthropic · claude-local
└─────────────────────────────┘
```

One install command. Open WebUI, the Claude Agent SDK, the OpenAI Agents
SDK, LiteLLM, and FastAPI are all bundled as required dependencies. No
optional extras for the runtime.

## A minimal AGENTS.md

```markdown
---
name: code-reviewer
description: Reviews a code diff. Ranks the top three issues by severity.
model: openrouter/anthropic/claude-haiku-4.5
---

You review code. When the user pastes a diff or a file, identify the top
three issues ranked by severity: correctness first, then security, then
readability.

For each issue, cite the line number and explain the fix in one sentence.
Skip style nits unless the user asks for them. If the code looks clean,
say so in one line and stop.
```

That is the whole hub. One file. No sub-agents, no skills, no knowledge
needed. Drop it in a folder, run `hubzoid run .`, and you have a code
reviewer at <http://localhost:3080>.

## Editing your hub

Your hub is one folder. Six things to know.

1. **Pick your model.** Default `.env` uses `MODEL=claude-local` (no key
   needed if `claude login` is done). To switch to OpenRouter / OpenAI /
   Anthropic, uncomment a stanza in `.env` and paste a key.
2. **Write the main agent.** Open `AGENTS.md`. The body is the system
   prompt. YAML frontmatter sets `name`, `description`, optional `model`,
   and optional `suggestions:` (a list of quick-start prompts shown as
   buttons on the empty chat screen).
3. **Sub-agents.** One folder per sub-agent under `agents/`. Each has its
   own `AGENTS.md`. Frontmatter `tools: [...]` whitelists which tools the
   sub-agent may call.
4. **Skills.** One folder per playbook under `skills/`, each with a
   `SKILL.md`. The main agent loads them on demand via `load_skill(name)`.
5. **Knowledge.** One markdown file per topic under `knowledge/`. Reached
   via `read_knowledge(name)`.
6. **Tools and connectors.** Drop Python files with `@function_tool` in
   `tools_local/`. Edit `connectors/.mcp.json` to plug in
   [MCP](https://modelcontextprotocol.io) servers.

Folder names are case- and plural-flexible. `skills/`, `Skills/`, and
`skill/` all work. Same for `agents/`, `knowledge/`, `tools_local/`,
`connectors/`. Restart with the same command. Changes are picked up on
the next start.

## Multi-hub agents repo

Run `hubzoid init` more than once in the same directory and you get a
Samarth-style multi-hub layout with one parent `requirements.txt`:

```bash
mkdir my-agents && cd my-agents
hubzoid init devops-agent       # creates ./devops-agent + ./requirements.txt + ./.gitignore + ./README.md
hubzoid init support-agent      # creates ./support-agent only; parent files left alone
hubzoid init research-agent     # creates ./research-agent only
```

Each hub is independent: its own `.env`, its own port, its own user
database. The parent files are written **only** on the first init in a
fresh directory (empty or containing only dotfiles / README /
requirements.txt / LICENSE). Idempotent and non-destructive afterward.

## Providers

Pick one stanza in `.env`. See [docs/providers.md](docs/providers.md) for
more detail.

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

# OR Claude local (uses your installed `claude` CLI + Pro/Max subscription)
# Requires `claude login` first. No API key needed.
MODEL=claude-local              # defaults to Haiku 4.5 (~3x faster TTFT than Sonnet)
# MODEL=claude-local/sonnet     # opt in to Sonnet
# MODEL=claude-local/opus       # opt in to Opus
# MODEL=claude-local/haiku      # explicit; same as bare `claude-local`
```

The `MODEL` string tells LiteLLM which provider to call, and the matching
key must be set. The exception is `MODEL=claude-local`: instead of
LiteLLM, hubzoid drives the Claude Agent SDK against your locally
installed `claude` CLI, so auth and billing flow through your existing
Pro/Max subscription. Same hub folder, same tools, same skills. Only the
LLM and auth path differ.

**Latency note on `claude-local`.** Requests go through the Claude Code
CLI, which adds ~1-2s per turn of harness overhead. If latency matters
more than subscription billing, use `anthropic/...` or
`openrouter/anthropic/...` with an API key — same models, no harness.

**OpenRouter tip.** If using `openrouter/anthropic/*`, pin Anthropic as the
preferred provider at [openrouter.ai/settings/preferences](https://openrouter.ai/settings/preferences)
(with fallbacks allowed). Hubzoid uses Anthropic prompt caching for ~70%
input-cost savings on multi-turn chats, but each upstream (Anthropic,
Vertex, Bedrock) has a separate cache pool, so cross-provider routing
fragments cache hits.

## Pre-shipped tools

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
| `render_jinja(template, context_json)` | Render a Jinja2 template. |
| `http_get(url)` | Fetch a URL (honors `HTTP_ALLOWLIST`, disable with `HUBZOID_DISABLE_HTTP_GET=true`). |
| `web_search(query)` | DuckDuckGo search. No API key. Disable with `HUBZOID_DISABLE_WEB_SEARCH=true`. |
| `current_time(zone)` | ISO 8601 timestamp in the given IANA timezone (default UTC). |

Custom tools dropped into `tools_local/*.py` are auto-discovered.

## MCP connectors

MCP connectors are **per-hub**. Each hub has its own
`<hub>/connectors/.mcp.json` alongside its `AGENTS.md`. Two hubs in the
same agents-repo connect to completely different MCP servers because each
loads its own config independently. There is no parent-level shared MCP
file by design: agents are independent products with their own scope.

Edit `demo-hub/connectors/.mcp.json` (or whatever your hub is named):

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
boot. The same `.mcp.json` is honored by both the OpenAI Agents and
Claude Agent runtimes.

## Branding and UI

Hubzoid passes ~24 env vars to Open WebUI to strip platform surfaces
(community sharing, code interpreter, multi-model arena, etc.) so the UI
reads as a single product. All flags are overridable from `.env`.

Per-hub identity has three knobs:

| Surface | Source |
|---|---|
| Top-bar name + tab title | `WEBUI_NAME` in `.env` (cascades to agent name, then `"Hubzoid"`) |
| Logo, favicon, splash | Drop files in `<hub>/branding/` (case-insensitive, multi-extension) |
| Quick-start prompts on the empty chat screen | `suggestions:` field in `AGENTS.md` frontmatter |

Full reference, including the override list for all 24 OWUI flags:
[docs/branding.md](docs/branding.md).

## Authentication

Default is single-user with no login - one user, localhost, no friction. For
multi-user or production, set `WEBUI_AUTH=true` and pick email + password or
an SSO provider: Google, Microsoft, GitHub, generic OIDC (Okta, Auth0,
Keycloak), or LDAP. Each agent runs its own user database, so adding a user
to one agent does not grant access to the others.

Full walkthrough including the Google Cloud Console click-path and the env
vars for every provider: [docs/auth.md](docs/auth.md).

## Deploying to production

`hubzoid run` is the production entry point. Wrap it in systemd (or a container) and put a reverse proxy in front for TLS. Full walkthrough: [docs/DEPLOYING.md](docs/DEPLOYING.md).

## Slack chat surface

Run any hub as a Slack bot. Users `@mention` it in a channel, DM it, or
chat from Slack's AI-assistant sidebar — same agent, same skills, same
knowledge, same `.env`. Uses **Socket Mode**, so no public URL or inbound
firewall changes are required.

```bash
hubzoid slack manifest my-hub > /tmp/manifest.json   # paste into api.slack.com
# drop SLACK_BOT_TOKEN and SLACK_APP_TOKEN into my-hub/.env, then one of:
hubzoid run my-hub --slack       # inline with the bridge + UI (one terminal)
hubzoid slack run my-hub         # or as a separate process (prod / systemd)
```

Full walkthrough (manifest, install, tokens, updating an existing app,
production systemd unit, troubleshooting): [docs/slack.md](docs/slack.md).

## CLI

```
hubzoid init [NAME]              Scaffold a new hub folder under the current directory.
                                   NAME defaults to "demo-hub".
                                   Also drops requirements.txt / .gitignore / README.md
                                   at the parent on first run if the directory looks fresh.
hubzoid run [PATH]               Start the FastAPI bridge plus Open WebUI for a hub.
  --port INT                       Open WebUI port (default 3080).
  --bridge-port INT                FastAPI bridge port (default 8000).
  --no-ui                          Bridge only, no Open WebUI.
  --slack, -s                      Also start the Slack adapter inline.
                                     Soft-warns if SLACK_BOT_TOKEN / SLACK_APP_TOKEN
                                     are missing; bridge + UI still come up.
hubzoid doctor [PATH]            Validate hub config and report issues.
hubzoid test [PATH]              Send one prompt to the agent and print the response.
hubzoid slack run [PATH]         Run the hub as a Slack bot (Socket Mode). See docs/slack.md.
hubzoid slack manifest [PATH]    Print a Slack App Manifest (JSON by default).
hubzoid slack systemd [PATH]     Print a systemd unit for the Slack adapter.
hubzoid version
hubzoid --help
```

PATH defaults to `.` for run / doctor / test. `python -m hubzoid ...` also
works as an alternative invocation.

## Run from source

For contributors or anyone who wants to read or extend the framework code.

```bash
git clone https://github.com/hubzoid/hubzoid.git
cd hubzoid
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
hubzoid run demo-hub
```

The repo ships with `demo-hub/` at the root as a working starter. Its
`.env` is git-ignored but the template includes sensible defaults
(`MODEL=claude-local`).

## Open standards

| Spec | Used at |
|---|---|
| [AGENTS.md](https://agents.md) | `<hub>/AGENTS.md`, `<hub>/agents/<n>/AGENTS.md` |
| SKILL.md | `<hub>/skills/<n>/SKILL.md` |
| [MCP](https://modelcontextprotocol.io) | `<hub>/connectors/.mcp.json` |

Hubs are portable across any tool that adopts these specs (Claude Code,
Cursor, Codex, Copilot, Gemini CLI, VS Code).

## Roadmap

* **v0.2** Current. PyPI release with bundled Open WebUI + Claude Agent
  SDK; OpenAI Agents and Claude Agent runtimes; AGENTS.md, SKILL.md, MCP
  loaders; OpenRouter, OpenAI, Anthropic, claude-local providers.
* **v0.3** Per-hub branding, auth-on path, native-venv production
  deployment docs, Playwright UI test tier.
* **v0.4** Slack chat surface (shipped — `hubzoid slack run`, see
  [docs/slack.md](docs/slack.md)). Background and scheduled workflows via
  WaveAssist Cloud (separate product, opt-in).
* **Later** Telegram chat surface. Mem0 / Zep memory backends.

Non-goals: voice and realtime, visual agent builder.

## Hubzoid as a service

This is the open-source framework. [hubzoid.com](https://hubzoid.com) is
the consulting practice that deploys role-scoped hubs for mid-enterprise
organizations in six weeks, fixed scope, fixed price. The framework is
the substrate; the practice ships the deployment.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs welcome.

## License

MIT.
