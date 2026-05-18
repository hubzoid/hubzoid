# Quickstart

Two paths, equivalent end state.

## Pip (recommended)

```bash
pip install hubzoid
hubzoid init demo-hub
hubzoid run demo-hub
```

`hubzoid init` scaffolds a working Hubzoid Guide agent. Default
`MODEL=claude-local` uses your installed `claude` CLI subscription, so no
API key is required if `claude login` is already done.

If you want a hosted provider instead, open `demo-hub/.env` and uncomment
one of the OpenRouter / OpenAI / Anthropic stanzas. Then re-run.

Open WebUI and the Claude Agent SDK are bundled with hubzoid. No extras
to install.

## Clone (contributors / read-the-source)

```bash
git clone https://github.com/hubzoid/hubzoid.git
cd hubzoid
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
hubzoid run demo-hub
```

The repo ships with `demo-hub/` already populated. Its `.env` is
git-ignored but the template includes sensible defaults
(`MODEL=claude-local`). Edit any of the files under `demo-hub/` to
customize, then restart.

## First chat

Open <http://localhost:3080>. Open WebUI lands you on a chat screen. The
model picker (top of chat) shows your hub's main agent. Say hi.

The default `demo-hub` is a Hubzoid Guide that explains the framework as
you ask it questions. Try: *"what is hubzoid?"*, *"show me the AGENTS.md
format"*, *"build me an agent for daily standup notes"*.

## What's wired

- **POST /v1/chat/completions** on `:8000`. OpenAI-compatible HTTP API.
- **GET /v1/models**. Single model entry matching your agent.
- **Open WebUI** on `:3080`. Points at the bridge as its OpenAI endpoint.
- **`output/<session>/`**. Anything your agent writes lands here.

## Troubleshooting

`hubzoid doctor demo-hub` runs every loader and reports issues with
line-level detail.

Common gotchas:

- "MODEL is not set" → no `.env` file or `MODEL=` is empty.
- "API key for X is not set" → key env var missing for the provider in `MODEL`.
- "unknown tool names" → a sub-agent's frontmatter references a tool that
  does not exist in pre-shipped or `tools_local/`.
