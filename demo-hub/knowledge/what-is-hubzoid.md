---
name: what-is-hubzoid
description: The canonical answer to "what is Hubzoid". One paragraph plus the product specifics, runtimes included.
keywords: [hubzoid, what is, product, overview, runtime, open source, mcp, workflows]
---

# What is Hubzoid

Hubzoid is one brain for your internal agents. Provide context once. Reuse
it across workflows, chat, and the AI tools your team already uses.

A **hub** is a versionable folder. It holds an `AGENTS.md` that defines the
main agent, plus optional knowledge, skills, sub-agent definitions, Python
tools, and MCP connectors. Hubzoid runs the folder. It handles the runtime,
the API, the chat surface, the streaming, and the routing between
sub-agents.

## One open-source product

Hubzoid is a single open-source product. All of it is Apache-2.0 licensed,
including team controls such as accounts, access rules, and the Admin
Console. There is no paid edition and no license key. The source lives at
`github.com/hubzoid/hubzoid`.

It is built for people who already get useful results from a personal
agent and want to give that agent to their team. Start on your laptop.
Move to a shared deployment when you are ready to manage accounts,
permissions, and ongoing operation.

Some teams want help building or operating their agents. Implementation
assistance is available through `hubzoid.com/enterprise`. It is an optional
service around the same open-source product, not a separate product or
edition.

## One hub, three ways to work

- **Chat.** Teammates ask questions and take authorized actions using the
  hub's context. The bundled web chat is Open WebUI. Slack and other
  channels each have their own setup.
- **Workflows.** Repeatable work on demand or on a schedule, with recorded
  runs. Markdown tasks live in `schedule/`. Python workflows live in
  `workflows/`.
- **Personal assistants through MCP.** A supported MCP client, such as
  Claude Code or Codex, connects to the hub and reuses its tools,
  knowledge, and skills under the hub's access rules. Opt in with
  `MCP_SERVER=true` in `.env`.

The three share the hub's context. Conversation history, workflow state,
and permissions stay separate.

## Three runtimes

`MODEL` in the hub's `.env` selects the execution backend.

| Runtime | `MODEL` value | What it needs |
|---|---|---|
| OpenAI Agents SDK, models through LiteLLM | A provider-prefixed name, such as `openrouter/anthropic/claude-haiku-4.5`, `openai/gpt-4o-mini`, or `anthropic/claude-haiku-4-5` | That provider's API key in `.env` |
| Claude Agent SDK | `claude-local` | Claude Code installed and signed in on the machine running the hub |
| Local Codex | `codex-local` | The pinned Codex CLI signed in with a file-backed login. macOS or Linux |

The same hub folder gives the same tools, skills, and knowledge on each
runtime. Provider and channel capabilities still vary. The setup guide is
`docs/providers.md` in the repository.

## What the product gives you

- **An OpenAI-compatible HTTP API** at `/v1/chat/completions`. Any
  OpenAI client works against it.
- **A bundled chat surface** (Open WebUI) on `:3080`. Multi-user,
  history, file uploads.
- **An Admin Console** at `/portal/` for agent access, restricted tools,
  and inspecting runs.
- **Pre-shipped tools.** File reads, knowledge reads, skill loads, HTTP,
  web search, Jinja rendering. Bring your own by dropping Python files
  into `tools_local/`.
- **MCP connectors.** Any MCP server works as a tool source. Configure in
  `connectors/.mcp.json`.
- **Sub-agents and handoffs.** Markdown-defined, runtime-routed.

## What you do not write

No runtime code. No FastAPI wiring. No chat UI work. No tool registration
boilerplate. No prompt-engineering scaffolding. The markdown is the IDE.

## Defaults

- A fresh interactive `hubzoid init` detects a signed-in Claude or Codex
  CLI and saves your choice. With no choice made, `MODEL` falls back to
  `claude-local`. Neither local runtime needs an API key. The CLI's own
  sign-in and usage limits apply.
- Open WebUI binds to `127.0.0.1:3080`. Not reachable from outside.
- Local single-user mode by default. Turn on authentication before a
  shared deployment.

## Scheduled work

Hubs run scheduled background work through Hubzoid's built-in scheduler. See
`schedule/` for tasks and `mcp-and-connectors` for external integrations.
