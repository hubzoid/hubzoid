# Providers

Hubzoid selects an execution backend from `MODEL` in the hub's `.env`:
`claude-local` uses the Claude Agent SDK with a locally authenticated Claude CLI;
`codex-local` uses the Codex app-server; provider-prefixed model names use the
OpenAI Agents SDK through LiteLLM.
Model precedence is `.env MODEL`, then the main `AGENTS.md` model, then
`claude-local`. The hub's tools, knowledge and access rules stay the same across
these backends.

## Local Claude

```dotenv
MODEL=claude-local
# MODEL=claude-local/sonnet
# MODEL=claude-local/opus
# MODEL=claude-local/haiku
```

Install and authenticate Claude Code on the machine running the hub. Fresh
interactive `hubzoid init` detects authenticated local Claude and Codex installs.
It selects the only usable CLI, or asks once when both are available. Existing
configuration is preserved. Non-interactive setup can pass `--model codex-local`
or configure `.env` explicitly. CLI installation alone does not establish
authentication or available usage.
`claude-local` is an SDK integration, not a shell command pasted into chat.

## Local Codex

```bash
npm install -g @openai/codex@0.147.0
codex -c 'cli_auth_credentials_store="file"' login
hubzoid init my-hub --model codex-local
```

```dotenv
MODEL=codex-local
# To pin an available model: MODEL=codex-local/<model-id>
```

Run login as the account that runs Hubzoid. This backend currently requires
macOS/Linux and file-backed authentication in `$CODEX_HOME/auth.json` (by default
`~/.codex/auth.json`). OS-keychain-only logins are not supported. `hubzoid doctor`
checks the pinned CLI and login. Account availability, model access and usage
limits still apply; login detection makes no paid model call.

The experimental app-server protocol is pinned to CLI **0.147.0**. Other versions
fail closed pending a tool-isolation review. Each request has an ephemeral thread,
an isolated configuration directory, and the hub's guarded tools. Personal MCP
servers, skills, hooks and settings are not inherited. Native shell, filesystem,
browser and web-search tools are disabled. Models requiring code mode use an
isolated V8 dispatcher with no module imports or native I/O; nested tool calls
still pass through Hubzoid access checks. The infrastructure login does not grant
end users administrator permissions. Temporary credentials and subprocesses are
removed after completion or cancellation; refreshed login tokens are saved back
without overwriting a concurrent operator login.

Streaming, structured `hub.call_llm`, hub tools and model-pinned delegates are
supported. Requests time out after five minutes; `max_turns` limits tool calls on
this backend. Tokens are reported; cost is an estimate only when a model price is
known, not a bill for subscription usage. The base Docker image does not install
or authenticate Codex: provision the pinned CLI and service-account login in a
custom image if using this backend in containers. Hosted API providers remain
the simpler container setup.

Codex can also use a hub as an [MCP client](mcp-server.md), independently of the
hub's chosen runtime. No custom client plugin is required. `codex -p` selects a
CLI profile; Hubzoid uses app-server, not `codex -p` as a prompt command.

## OpenRouter

Use an OpenRouter key and a model identifier available to your account.

```bash
OPENROUTER_API_KEY=sk-or-v1-...
MODEL=openrouter/anthropic/claude-haiku-4.5
# Examples:
# MODEL=openrouter/openai/gpt-4o-mini
# MODEL=openrouter/meta-llama/llama-3.1-70b-instruct
# MODEL=openrouter/google/gemini-2.0-flash-001
```

Browse models at https://openrouter.ai/models. Prefix with `openrouter/`.

## OpenAI direct

```bash
OPENAI_API_KEY=sk-...
MODEL=openai/gpt-4o-mini
# Other examples:
# MODEL=openai/gpt-4o
# MODEL=openai/o3-mini
```

## Anthropic direct

```bash
ANTHROPIC_API_KEY=sk-ant-...
MODEL=anthropic/claude-haiku-4-5
# Other examples:
# MODEL=anthropic/claude-sonnet-4-6
# MODEL=anthropic/claude-opus-4-7
```

## Per-agent overrides

An agent can declare `model:` in frontmatter. A sub-agent with a different
model in the same backend can run as a delegate; cross-backend declarations
fall back to skills rather than silently switching runtimes. For example:

```markdown
---
name: triage
description: Decide which deep agent to hand off to.
model: openrouter/openai/gpt-4o-mini
tools: []
---
```

## Other LiteLLM providers

LiteLLM supports Azure OpenAI, AWS Bedrock, Google Vertex, Groq, Together,
Fireworks, Cohere, Mistral, Replicate, Ollama, and more. The `MODEL` prefix
plus the corresponding env vars (see LiteLLM docs) select the provider. Availability and tool support vary by model; validate
your selected model with your hub before deploying.
