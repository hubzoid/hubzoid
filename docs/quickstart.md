# Quickstart

Two paths, equivalent end state.

## Clone

```bash
git clone https://github.com/your-org/hubzoid.git
cd hubzoid
pip install -r requirements.txt
python -m hubzoid run my-hub
```

The repo ships with `my-hub/` already populated. Open `my-hub/` and edit:

1. Copy `.env.example` → `.env`. Paste a key. Pick a model.
2. Edit `AGENTS.md`. The body is your system prompt.

Restart with the same command.

## Pip

```bash
pip install 'hubzoid[ui]'
hubzoid init my-hub
hubzoid run my-hub
```

`hubzoid init` writes the same starter files anywhere on disk.

## First chat

Open http://localhost:3080. Open WebUI lands you on a chat screen. The model
picker (top of chat) shows your hub's main agent. Send "hello".

## What's wired

- **POST /v1/chat/completions** on `:8000`. OpenAI-compatible HTTP API.
- **GET /v1/models**. single model entry matching your agent.
- **Open WebUI** on `:3080`. points at the bridge as its OpenAI endpoint.
- **`output/<session>/`**. anything your agent writes lands here.

## Troubleshooting

`hubzoid doctor my-hub`. runs every loader and reports issues with line-level
detail.

Common gotchas:

- "MODEL is not set" → no `.env` file or `MODEL=` is empty.
- "API key for X is not set" → key env var missing for the provider in `MODEL`.
- "unknown tool names" → a sub-agent's frontmatter references a tool that
  doesn't exist in pre-shipped or `tools_local/`.
