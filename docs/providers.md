# Providers

Hubzoid selects an execution backend from `MODEL` in the hub's `.env`:
`claude-local` uses the Claude Agent SDK with a locally authenticated Claude CLI;
`codex-local` uses the Codex app-server; `bedrock/<tier-or-id>` runs Claude on
Amazon Bedrock through the Claude Agent SDK ([Amazon Bedrock](bedrock.md));
other provider-prefixed model names use the OpenAI Agents SDK through LiteLLM.
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
npm install -g @openai/codex@0.163.0      # or 0.147.0
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
checks the CLI version and login. Account availability, model access and usage
limits still apply; login detection makes no paid model call.

The experimental app-server protocol is reviewed for CLI **0.147.0** and
**0.163.0**. Other versions fail closed with the versions to install, pending a
tool-isolation review. For 0.163.0 Hubzoid turns off the features that review
found could reach outside the hub (plugins, browser and computer use, worktrees,
background terminals, MCP apps and similar) on every request. Set
`HUBZOID_CODEX_BIN` to use a Codex binary that is not first on `PATH`. Each request has an ephemeral thread,
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

## Amazon Bedrock

Bedrock is opt-in. `claude-local` with a Claude subscription stays the default,
and a hub uses Bedrock only when you set it. To run a hub's models in your own
AWS account, add to its `.env`:

```dotenv
MODEL=bedrock/sonnet
AWS_REGION=ap-south-1
```

- `bedrock/sonnet`, `bedrock/opus`, `bedrock/haiku` or a Bedrock Claude id run
  Claude through the Claude Agent SDK, with the same tools, skills and chat as
  `claude-local`. Other Bedrock models, such as Nova, run through LiteLLM.
- The hub needs a region and AWS credentials: an instance or task role,
  `AWS_PROFILE`, or a Bedrock API key. The role needs
  `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the
  models it uses, plus the lookups `hubzoid doctor` makes. The full IAM policy
  is on the [Amazon Bedrock](bedrock.md) page.
- Claude tiers use your region's inference profiles (`us.`, `eu.`, `apac.`).
  Hubzoid never switches to `global.` profiles by itself. What a region offers
  varies, so run `hubzoid doctor`, which lists the prefixes your account has.
- Web search is not available on Bedrock, and credential failover between
  subscription tokens does not apply.
- On a Bedrock hub, `model: claude-local/opus` in a task keeps that task on the
  subscription, so one hub can use both.

## Other LiteLLM providers

LiteLLM supports Azure OpenAI, Google Vertex, Groq, Together,
Fireworks, Cohere, Mistral, Replicate, Ollama, and more. Amazon Bedrock has its
own page, [Amazon Bedrock](bedrock.md): a Claude id under `bedrock/` runs on
the Claude Agent SDK, other Bedrock models on LiteLLM. The `MODEL` prefix
plus the corresponding env vars (see LiteLLM docs) select the provider. Availability and tool support vary by model; validate
your selected model with your hub before deploying.
