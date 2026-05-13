---
name: about_hubzoid
description: What hubzoid is, surfaced when the user asks about the platform itself.
keywords: [hubzoid, platform, about]
---

# About hubzoid

hubzoid turns a folder of markdown files into a running AI agent. The runtime
is the OpenAI Agents SDK; the API is OpenAI-compatible; the chat front-end is
Open WebUI. Provider-agnostic via LiteLLM — OpenRouter, OpenAI, and
Anthropic supported out of the box.

A hub is one folder containing:

- `AGENTS.md`             — the main agent's instructions
- `agents/<name>/AGENTS.md` — optional sub-agents (handoffs)
- `skills/<name>/SKILL.md` — optional playbooks (loaded on demand)
- `knowledge/<topic>.md`  — optional long-form domain content
- `connectors/.mcp.json`  — optional MCP server config
- `tools_local/*.py`      — optional custom Python tools
- `.env`                  — keys + model id

That folder is the whole project. Drop it anywhere on disk and run
`hubzoid run`.
