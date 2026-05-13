# Quickstart

Two paths, equivalent end state.

## Clone

```bash
git clone https://github.com/hubzoid/hubzoid.git
cd hubzoid
pip install -r requirements.txt
```

The repo ships with `my-hub/` already populated. Set it up before running:

```bash
cp my-hub/.env.example my-hub/.env
```

1. Open `my-hub/.env`. Paste a key from OpenRouter, OpenAI, or Anthropic. Pick a model. The file shows you how.
2. Open `my-hub/AGENTS.md`. The body is your system prompt. Edit it or keep the starter.
3. (Optional) Drop more files into `my-hub/agents/`, `skills/`, `knowledge/`.

Now run:

```bash
python -m hubzoid run my-hub
```

Restart with the same command after any change.

## Pip

```bash
pip install 'hubzoid[ui]'
hubzoid init my-hub
cp my-hub/.env.example my-hub/.env
```

Then edit `my-hub/.env` (key + model) and `my-hub/AGENTS.md` (your prompt). Run:

```bash
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
