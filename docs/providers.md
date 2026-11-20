# Providers

`MODEL` in the hub's `.env` picks the model and the runtime that runs it. The
hub's tools, knowledge and access rules stay the same on every runtime. Without
`MODEL`, Hubzoid uses the main `AGENTS.md` `model:`, then `claude-local`.

```dotenv
MODEL=claude-local
```

| `MODEL=` | Runtime | Sign in with | Details |
|---|---|---|---|
| `claude-local` (default), `claude-local/sonnet`, `claude-local/opus`, `claude-local/haiku` | Claude Agent SDK | A Claude Code login on the machine | [Local Claude](#local-claude) |
| `codex-local`, `codex-local/<model-id>` | Codex app-server | A file-backed Codex login | [Local Codex](#local-codex) |
| `bedrock/sonnet`, `bedrock/opus`, `bedrock/haiku`, a Bedrock id or ARN | Claude Agent SDK for Claude, LiteLLM for other models | AWS credentials and a region | [Amazon Bedrock](bedrock.md) |
| `openrouter/<id>`, `openai/<id>`, `anthropic/<id>` and other LiteLLM prefixes | OpenAI Agents SDK through LiteLLM | The provider's API key | [API providers](#api-providers) |

## Local Claude

```dotenv
MODEL=claude-local
# MODEL=claude-local/opus
# MODEL=claude-local/haiku
```

Bare `claude-local` runs Sonnet. Install and sign in to Claude Code on the
machine that runs the hub. `claude-local` is an SDK integration, not a shell
command pasted into chat.

Interactive `hubzoid init` detects signed-in Claude Code and Codex installs. It
picks the only usable one, or asks once when both are available, and keeps
existing configuration. For non-interactive setup, pass `--model codex-local` or
set `.env` yourself. An installed CLI alone does not mean it is signed in or has
usage left.

## Local Codex

```bash
npm install -g @openai/codex@0.163.0      # or 0.147.0
codex -c 'cli_auth_credentials_store="file"' login
hubzoid init my-hub --model codex-local
```

```dotenv
MODEL=codex-local
# To pin an available model: MODEL=codex-local/<model-id>
```

| Setting | Default | What it does |
|---|---|---|
| `HUBZOID_CODEX_BIN` | `codex` on `PATH` | The Codex binary to run. |
| `CODEX_HOME` | `~/.codex` | Where the login lives (`$CODEX_HOME/auth.json`). |

Requirements:

- macOS or Linux, and a file-backed login in `$CODEX_HOME/auth.json`. Log in as
  the account that runs Hubzoid. Logins kept only in the OS keychain are not
  supported.
- Codex CLI 0.147.0 or 0.163.0. These are the versions whose app-server
  protocol has been reviewed. Other versions fail closed and name the versions
  to install.
- `hubzoid doctor` checks the CLI version and login. It makes no paid model
  call, so account availability, model access and usage limits still apply.

What Hubzoid isolates:

- Each request has an ephemeral thread, its own configuration directory and the
  hub's guarded tools. Personal MCP servers, skills, hooks and settings are not
  inherited.
- Native shell, filesystem, browser and web search tools are off. For 0.163.0
  Hubzoid also turns off, on every request, the features the review found could
  reach outside the hub (plugins, browser and computer use, worktrees,
  background terminals, MCP apps and similar).
- Models that need code mode use an isolated V8 dispatcher with no module
  imports or native I/O. Nested tool calls still pass Hubzoid's access checks.
- The infrastructure login does not give end users administrator permissions.
  Temporary credentials and subprocesses are removed after completion or
  cancellation. Refreshed login tokens are saved back without overwriting a
  concurrent operator login.

What works and the limits:

- Streaming, structured `hub.call_llm`, hub tools and model-pinned delegates.
- A request times out after ten minutes. `max_turns` limits tool calls on this
  runtime.
- Tokens are reported. Cost is an estimate only when a model price is known,
  and is not a bill for subscription usage.
- The base Docker image does not install or sign in to Codex. To use Codex in
  containers, add the pinned CLI and a service account login in a custom image.
  API providers are the simpler container setup.

Codex can also use a hub as an [MCP client](mcp-server.md), whatever runtime the
hub uses. No client plugin is needed. `codex -p` selects a CLI profile. Hubzoid
uses app-server, not `codex -p` as a prompt command.

## Amazon Bedrock

Bedrock runs a hub's models in your own AWS account. It is opt in, and
`claude-local` stays the default.

```dotenv
MODEL=bedrock/sonnet
AWS_REGION=ap-south-1
```

A Claude model on Bedrock runs on the Claude Agent SDK, with the same tools,
skills and chat as `claude-local`. Other Bedrock models, such as Nova, run on
LiteLLM. The hub needs a region and AWS credentials, and web search is not
available. The [Amazon Bedrock](bedrock.md) page has the model ids, settings,
IAM policy, costs and limitations.

## API providers

Any LiteLLM provider works with its `MODEL` prefix and API key. Availability and
tool support vary by model, so check your model with your hub before you deploy.

```dotenv
# OpenRouter: browse ids at https://openrouter.ai/models
OPENROUTER_API_KEY=sk-or-v1-...
MODEL=openrouter/anthropic/claude-haiku-4.5

# OpenAI
# OPENAI_API_KEY=sk-...
# MODEL=openai/gpt-4o-mini

# Anthropic API
# ANTHROPIC_API_KEY=sk-ant-...
# MODEL=anthropic/claude-sonnet-4-6
```

LiteLLM also supports Azure OpenAI, Google Vertex, Groq, Together, Fireworks,
Cohere, Mistral, Replicate, Ollama and more. See the LiteLLM docs for each
provider's prefix and environment variables.

## Use another model for one agent or task

A sub-agent can set `model:` in its frontmatter:

```markdown
---
name: triage
description: Decide which deep agent to hand off to.
model: claude-local/haiku
tools: []
---
```

- On a Bedrock hub, a bare Claude name (`opus`, `sonnet`, `haiku`, a
  `claude-*` id) follows the hub onto Bedrock. On a `claude-local` hub, write
  the prefix (`claude-local/haiku`) for a sub-agent. A scheduled task's
  `model:` and `hub.call_llm(model=...)` accept the bare name on either.
- An id that names its provider is used as written. `claude-local/opus` on a
  Bedrock hub runs on the subscription, and `bedrock/opus` on a `claude-local`
  hub runs on Bedrock. See [Amazon Bedrock](bedrock.md#use-bedrock-and-the-subscription-together).
- A sub-agent whose model is on the same runtime and provider as the hub, but
  differs from the hub's model, runs as its own delegate. On another runtime or
  provider it is loaded as instructions for the main agent instead of switching
  runtimes. `hubzoid doctor` shows how each sub-agent runs (`agents.mode`).
