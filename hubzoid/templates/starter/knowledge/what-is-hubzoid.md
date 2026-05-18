---
name: what-is-hubzoid
description: The canonical answer to "what is Hubzoid". One paragraph plus the framework specifics.
keywords: [hubzoid, what is, framework, overview]
---

# What is Hubzoid

Hubzoid turns a folder of markdown files into a running AI agent with a
polished web UI.

You write three kinds of markdown: an `AGENTS.md` that defines the agent,
optional `skills/<name>/SKILL.md` playbooks loaded on demand, and optional
`knowledge/<topic>.md` files the agent can read. Hubzoid handles the
runtime, the API, the chat surface, the streaming, and the routing between
sub-agents.

## Two layers

Hubzoid is two things layered on top of each other.

1. **The open-source framework.** The Python package you just installed.
   MIT licensed. The substrate. Lives at `github.com/hubzoid/hubzoid`.
2. **The Hubzoid consulting practice.** A service company that deploys
   role-scoped hubs for mid-enterprise organizations in six weeks, fixed
   scope, fixed price. Lives at `hubzoid.com`.

If you are reading this, you are using the framework. The consulting
practice is built on top of it.

## What the framework gives you

- **One runtime, two engines.** Default engine is the OpenAI Agents SDK
  with LiteLLM as the provider layer (OpenRouter, OpenAI, Anthropic, any
  LiteLLM-supported provider). Alternate engine is the Claude Agent SDK
  for users running on a `claude` CLI subscription. Same hub folder runs
  on either.
- **An OpenAI-compatible HTTP API** at `/v1/chat/completions`. Any
  OpenAI client works against it.
- **A bundled chat surface** (Open WebUI) on `:3080`. Multi-user,
  history, file uploads, voice in and out via the browser.
- **Pre-shipped tools.** File ops, knowledge reads, skill loads, HTTP,
  web search, Jinja rendering, persistent memory. Bring your own by
  dropping Python files into `tools_local/`.
- **MCP support.** Any MCP server works as a tool source. Configure in
  `connectors/.mcp.json`.
- **Sub-agents and handoffs.** Markdown-defined, runtime-routed.

## What you do not write

No runtime code. No FastAPI wiring. No chat UI work. No tool registration
boilerplate. No prompt-engineering scaffolding. The markdown is the IDE.

## Defaults

- `MODEL=claude-local` is the default in scaffolded hubs. No API key
  needed. Requires `claude` CLI installed and logged in.
- Open WebUI binds to `127.0.0.1:3080`. Not reachable from outside.
- Auth off by default for local dev. Turn on for production deployment.

## Sister product

Hubs that need scheduled background work (the third agent type) hand off
to WaveAssist Cloud at run time. WaveAssist is an internal infrastructure
choice. Customers see Hubzoid. See `mcp-and-connectors` for how the two
pieces fit together at the integration layer.
