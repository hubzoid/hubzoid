<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/hubzoid/hubzoid/main/assets/mark-dark.svg">
    <img alt="/hubzoid" src="https://raw.githubusercontent.com/hubzoid/hubzoid/main/assets/mark-light.svg" width="220">
  </picture>
</p>

<p align="center">
  <strong>Any chat surface &rarr; your agent &rarr; any SDK. Defined in markdown.</strong><br>
  <sub>An open-source framework for production AI agents. Deployed inside your perimeter. The substrate behind <a href="https://hubzoid.com">Hubzoid</a> customer deployments.</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/hubzoid/"><img src="https://img.shields.io/pypi/v/hubzoid?color=E5572A&label=pypi" alt="PyPI"></a>
  <a href="https://pypi.org/project/hubzoid/"><img src="https://img.shields.io/pypi/pyversions/hubzoid?color=0B0B0C" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-0B0B0C" alt="MIT License"></a>
  <a href="https://hubzoid.com"><img src="https://img.shields.io/badge/website-hubzoid.com-E5572A" alt="hubzoid.com"></a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/hero-map-dark.svg">
    <img alt="Open WebUI, Slack, WhatsApp, Telegram and more, through Hubzoid, onto the Claude Agent SDK, OpenAI Agents SDK, LiteLLM and more" src="assets/hero-map-light.svg" width="900">
  </picture>
</p>

---

## What is Hubzoid

Hubzoid is the layer between your chat surfaces and your agent runtime. You write
a folder of markdown -- `AGENTS.md`, `agents/`, `skills/`, `knowledge/` -- and
Hubzoid turns it into a running agent, served over an OpenAI-compatible HTTP API
and reachable from Open WebUI, Slack, WhatsApp, and Telegram at once.

You write the markdown. Hubzoid handles the runtime, the API, the UI, the
streaming, and the sub-agent routing. The engine underneath is the [OpenAI Agents
SDK](https://openai.github.io/openai-agents-python/) or the [Claude Agent
SDK](https://code.claude.com/docs/en/agent-sdk/overview), provider-agnostic via
[LiteLLM](https://docs.litellm.ai) -- OpenRouter, OpenAI, Anthropic, and local
Claude work out of the box.

## Quickstart

**3 steps if you have `claude` CLI installed and logged in, 4 otherwise.**

```bash
pip install hubzoid
hubzoid init my-hub                    # minimal runnable hub + agents-repo wrapper
*  edit my-hub/.env                    # ← optional. skip if using claude-local.
hubzoid run my-hub
```

Open <http://localhost:3080>. You get a generic assistant with one example of
every Hubzoid surface (one skill, one knowledge file, one sub-agent, one custom
tool) so the folder layout is obvious. Edit `my-hub/AGENTS.md` to make it yours.

Want the guided tour instead? `hubzoid init my-hub --template demo` gives a
**Hubzoid Guide** agent that explains the framework as you chat.

<details>
<summary>Python version and build caveats</summary>

> Python 3.11 or 3.12 (Open WebUI does not yet support 3.13+). On recent macOS,
> the default `python3` is too new; create your venv with `python3.12 -m venv`
> explicitly. If pip tries to build `av` (PyAV) from source, run
> `brew install pkg-config ffmpeg` first.

**\* Step 3 (the optional one).** Default `MODEL=claude-local` uses your installed
`claude` CLI subscription. If you already ran `claude login`, skip this step and
go straight to `hubzoid run`. Otherwise open `my-hub/.env`, comment out the
`MODEL=claude-local` line, and uncomment one provider stanza (OpenRouter, OpenAI,
Anthropic) with your key pasted in.

The two files you edit later as you customize:

1. `my-hub/.env`: keys, model selection, UI knobs.
2. `my-hub/AGENTS.md`: the system prompt body. YAML frontmatter sets `name`,
   `description`, and optional `model`.

</details>

## One agent, every surface

Same agent, same skills, same knowledge, same `.env`. Pick the surfaces you want.

| Surface | Status | Get started |
|---|---|---|
| Open WebUI (web chat, white-label) | Shipped | bundled with `hubzoid run` |
| Slack (Socket Mode, no public URL) | Shipped | [docs/slack.md](docs/slack.md) |
| WhatsApp (webhook) | Shipped | [docs/inbound-surfaces.md](docs/inbound-surfaces.md) |
| Telegram (webhook, streaming) | Shipped | [docs/inbound-surfaces.md](docs/inbound-surfaces.md) |
| MCP server (serve your hub to other AIs) | Shipped | [docs/mcp-server.md](docs/mcp-server.md) |
| More surfaces | Coming | see [Roadmap](#roadmap) |

```bash
hubzoid run my-hub --slack --whatsapp --telegram   # any combination, one process
```

Slack uses Socket Mode (no public URL). WhatsApp and Telegram use an inbound
webhook and a per-hub `identity/access.csv` roster that maps each sender to an
email + groups, so only registered people get a reply. Conversation memory,
per-sender identity, access gating, and typing/read receipts are built in.

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

That is the whole hub. One file. No sub-agents, no skills, no knowledge needed.
Drop it in a folder, run `hubzoid run .`, and you have a code reviewer at
<http://localhost:3080> -- and, if you flip on the surfaces above, in Slack,
WhatsApp, and Telegram too.

## How it works

```
┌─────────────────────────────┐
│  Surfaces                   │  Open WebUI · Slack · WhatsApp · Telegram
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

One install command. Open WebUI, the Claude Agent SDK, the OpenAI Agents SDK,
LiteLLM, and FastAPI are all bundled as required dependencies. No optional extras
for the runtime.

## Editing your hub

Your hub is one folder. The pieces you can add:

1. **Pick your model.** Default `.env` uses `MODEL=claude-local` (no key needed if
   `claude login` is done). To switch to OpenRouter / OpenAI / Anthropic,
   uncomment a stanza in `.env` and paste a key.
2. **Write the main agent.** `AGENTS.md` body is the system prompt. Frontmatter
   sets `name`, `description`, optional `model`, and optional `suggestions:`
   (quick-start prompts shown as buttons on the empty chat screen).
3. **Sub-agents.** One folder per sub-agent under `agents/`, each with its own
   `AGENTS.md`. Frontmatter `tools: [...]` whitelists which tools it may call.
4. **Skills.** One folder per playbook under `skills/`, each a `SKILL.md`. Loaded
   on demand via `load_skill(name)`.
5. **Knowledge.** One markdown file per topic under `knowledge/`, reached via
   `read_knowledge(name)`.
6. **Tools and connectors.** Drop Python files with `@function_tool` in
   `tools_local/`. Edit `connectors/.mcp.json` to plug in
   [MCP](https://modelcontextprotocol.io) servers.
7. **Unstructured data.** Drop code repos or document dumps into `raw_data/`. The
   agent searches it with `grep_data` and reads files with `read_file`. No
   indexing step -- the folder ships with the hub.
8. **Scheduled tasks.** One markdown file per background job under `schedule/`.
   Frontmatter sets the cron cadence; the body is plain-English instructions the
   hub's own agent runs unattended while `hubzoid run` is up. See
   [docs/schedule.md](docs/schedule.md).
9. **Evals.** One markdown file per behavioural check under `evals/` -- a prompt
   plus what the answer must do. Run by hand, from CI (exit code is the gate), or
   on a cron. See [docs/evals.md](docs/evals.md).

Folder names are case- and plural-flexible (`skills/`, `Skills/`, `skill/` all
work). Changes are picked up on the next start.

<details>
<summary>Multi-hub agents repo</summary>

Run `hubzoid init` more than once in the same directory and you get a
Samarth-style multi-hub layout with one parent `requirements.txt`:

```bash
mkdir my-agents && cd my-agents
hubzoid init devops-agent       # creates ./devops-agent + ./requirements.txt + ./.gitignore + ./README.md
hubzoid init support-agent      # creates ./support-agent only; parent files left alone
hubzoid init research-agent     # creates ./research-agent only
```

Each hub is independent: its own `.env`, its own port, its own user database. The
parent files are written **only** on the first init in a fresh directory.
Idempotent and non-destructive afterward.

</details>

<details>
<summary>Providers (.env stanzas)</summary>

Pick one stanza in `.env`. See [docs/providers.md](docs/providers.md) for detail.

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
```

The `MODEL` string tells LiteLLM which provider to call, and the matching key
must be set. The exception is `MODEL=claude-local`: instead of LiteLLM, Hubzoid
drives the Claude Agent SDK against your locally installed `claude` CLI, so auth
and billing flow through your existing Pro/Max subscription.

**Latency note on `claude-local`.** Requests go through the Claude Code CLI, which
adds ~1-2s per turn of harness overhead. If latency matters more than
subscription billing, use `anthropic/...` or `openrouter/anthropic/...` with an
API key -- same models, no harness.

**OpenRouter tip.** If using `openrouter/anthropic/*`, pin Anthropic as the
preferred provider at
[openrouter.ai/settings/preferences](https://openrouter.ai/settings/preferences).
Hubzoid uses Anthropic prompt caching for ~70% input-cost savings, but each
upstream has a separate cache pool, so cross-provider routing fragments cache
hits.

</details>

<details>
<summary>Pre-shipped tools</summary>

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
| `http_get(url)` | Fetch a URL (honors `HTTP_ALLOWLIST`). |
| `web_search(query)` | DuckDuckGo search. No API key. |
| `current_time(zone)` | ISO 8601 timestamp in the given IANA timezone. |

Custom tools dropped into `tools_local/*.py` are auto-discovered.

</details>

<details>
<summary>MCP -- consume connectors and serve your hub</summary>

**Consume.** MCP connectors are per-hub. Each hub has its own
`<hub>/connectors/.mcp.json`. `${VAR}` references resolve against the environment
at boot. Honored by both the OpenAI Agents and Claude Agent runtimes.

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

**Serve.** A hub can *be* an MCP server, so people connect from their own AI
(Claude Code, Cursor) and use the hub's tools and knowledge with their own model.

```dotenv
# <hub>/.env
MCP_SERVER=true
```

The bridge then serves Streamable HTTP MCP at `/mcp`. Every call runs under the
caller's identity, `restricted/` tools follow the same group rules as chat, and
every decision is audited. Details: [docs/mcp-server.md](docs/mcp-server.md).

</details>

<details>
<summary>Branding, auth, and access control</summary>

**Branding.** Hubzoid passes ~24 env vars to Open WebUI to strip platform
surfaces so the UI reads as a single product. Per-hub identity: `WEBUI_NAME` for
the top-bar name, drop files in `<hub>/branding/` for logo/favicon/splash,
`suggestions:` in `AGENTS.md` for the empty-chat prompts. Full reference:
[docs/branding.md](docs/branding.md).

**Authentication.** Default is single-user, no login. For production, set
`WEBUI_AUTH=true` and pick email + password or SSO (Google, Microsoft, GitHub,
generic OIDC, LDAP). Each agent runs its own user database. Full walkthrough:
[docs/auth.md](docs/auth.md).

**Access control.** Put a sensitive tool in a `restricted/` folder and its file
name becomes a permission; an Open WebUI group of the same name is the key. The
runtime hides tools a user may not use and fails closed if one is reached anyway,
logging every decision (`hubzoid audit <hub>`). Entirely opt-in. Full guide:
[docs/access-management.md](docs/access-management.md).

</details>

<details>
<summary>Deploying to production</summary>

`hubzoid run` is the production entry point. Wrap it in systemd (or a container)
and put a reverse proxy in front for TLS. Only the one Open WebUI port needs to
be exposed -- the built-in edge router serves artifact downloads off the loopback
bridge through that same port, so set `HUBZOID_PUBLIC_URL=https://your.host` in
`<hub>/.env` and download links just work. Running a hub per team on one box?
`hubzoid gateway` puts them behind a single Open WebUI. Full walkthrough:
[docs/DEPLOYING.md](docs/DEPLOYING.md).

</details>

<details>
<summary>CLI reference</summary>

```
hubzoid init [NAME]              Scaffold a new hub folder under the current directory.
  --template, -t NAME              "minimal" (default) or "demo" (guided tour).
hubzoid run [PATH]               Start the FastAPI bridge plus Open WebUI for a hub.
  --port INT                       Public Open WebUI port (default 3080).
  --bridge-port INT                FastAPI bridge port (default 8000, loopback).
  --no-ui                          Bridge only, no Open WebUI / edge.
  --slack, -s                      Also start the Slack adapter inline.
  --whatsapp / --telegram          Also start the inbound webhook surfaces inline.
hubzoid gateway [HUBS...]        One shared Open WebUI fronting many hub bridges.
hubzoid schedule list [PATH]     List the hub's scheduled tasks + next fire times.
hubzoid schedule run PATH TASK   Fire one task NOW, in-process.
hubzoid schedule status [PATH]   Show recorded fire history per task.
hubzoid eval run [PATH]          Run evals/*.md against the hub's agent (exit code = CI gate).
hubzoid eval list/status/explain Inspect and debug eval cases.
hubzoid doctor [PATH]            Validate hub config and report issues.
hubzoid audit [PATH]             Show the access log for restricted tools.
hubzoid test [PATH]              Send one prompt to the agent and print the response.
hubzoid slack run/manifest/systemd [PATH]     Run the hub as a Slack bot. See docs/slack.md.
hubzoid inbound run/systemd [PATH]            Serve the WhatsApp/Telegram webhook app.
hubzoid version
hubzoid --help
```

PATH defaults to `.` for run / doctor / test. `python -m hubzoid ...` also works.

</details>

<details>
<summary>Run from source</summary>

```bash
git clone https://github.com/hubzoid/hubzoid.git
cd hubzoid
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
hubzoid run demo-hub
```

The repo ships with `demo-hub/` at the root as a working starter. Its `.env` is
git-ignored but the template includes sensible defaults (`MODEL=claude-local`).

</details>

## Open standards

| Spec | Used at |
|---|---|
| [AGENTS.md](https://agents.md) | `<hub>/AGENTS.md`, `<hub>/agents/<n>/AGENTS.md` |
| SKILL.md | `<hub>/skills/<n>/SKILL.md` |
| [MCP](https://modelcontextprotocol.io) | `<hub>/connectors/.mcp.json` (consume) · `/mcp` endpoint (serve) |

Hubs are portable across any tool that adopts these specs (Claude Code, Cursor,
Codex, Copilot, Gemini CLI, VS Code).

## Roadmap

Shipped so far: bundled Open WebUI + Claude Agent SDK and OpenAI Agents runtimes;
AGENTS.md / SKILL.md / MCP loaders; OpenRouter, OpenAI, Anthropic, claude-local
providers; per-hub branding and auth; Slack, WhatsApp, and Telegram surfaces with
per-sender identity, access gating, and conversation memory; scheduled tasks and
evals; MCP server mode.

**Coming next:**

* More chat surfaces (Gmail and others).
* Mem0 / Zep memory backends.

Non-goals: voice and realtime, visual agent builder.

## Hubzoid as a service

This is the open-source framework. [hubzoid.com](https://hubzoid.com) is the
consulting practice that deploys role-scoped hubs for mid-enterprise
organizations in six weeks, fixed scope, fixed price. The framework is the
substrate; the practice ships the deployment.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs welcome.

## License

MIT -- all of it. The "Hubzoid" name and logo are trademarks of WaveAssist
Technologies Pvt Ltd. See [LICENSING.md](LICENSING.md).
