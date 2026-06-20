# Branding and UI configuration

Hubzoid wraps Open WebUI but configures it so the customer sees a single
product, not "Open WebUI hosting a model". This page documents what
hubzoid sets and how to override anything.

## Per-hub identity

Three knobs control the agent's visible identity. Two live in `.env`,
one lives in a folder.

| Surface | Source | Default |
|---|---|---|
| Top-bar name + tab title | `WEBUI_NAME` in `.env` | Cascades: `WEBUI_NAME` → `MODEL_LABEL` → main agent's `name:` from `AGENTS.md` → `"Hubzoid"` |
| Watermark on copied messages | `RESPONSE_WATERMARK` in `.env` | Hub folder name |
| Logo, favicon, splash | `<hub>/branding/` folder (see below) | Open WebUI defaults |

### Branding folder

Drop files into `<hub>/branding/`. Filenames are case-insensitive. Open
WebUI's defaults render for any slot you leave empty.

| Slot | Accepted filenames |
|---|---|
| logo | `logo.{svg,png,webp,jpg,jpeg}` |
| favicon | `favicon.{svg,ico,png}` |
| splash | `splash.{png,svg,webp,jpg,jpeg}` |

If both `logo.*` and `favicon.*` exist, favicon wins. Open WebUI uses one
mark in both top-bar and tab positions.

The demo-hub template ships sample Hubzoid SVGs. Replace them with your
own, or delete them to fall back to Open WebUI's defaults.

### Static surfaces (tab title, link previews, PWA name)

`WEBUI_NAME` only renames what the SPA re-renders in the browser at
runtime. Three surfaces are served as **static files** that ship as
"Open WebUI" and never see `WEBUI_NAME`:

- the `<title>` a browser tab shows before the app hydrates,
- the `<meta>` description/OpenGraph a link-preview crawler reads when
  someone pastes the hub URL into Slack/iMessage/etc.,
- the PWA `site.webmanifest` name.

Hubzoid rewrites all three on every `hubzoid run`, using the same
resolved name as the cascade above (so the default is `"Hubzoid"`, never
bare "Open WebUI"). This is the same license-gated debrand as the
`(Open WebUI)` suffix: operators above Open WebUI's 50-user threshold set
`HUBZOID_KEEP_OWUI_SUFFIX=True`, which keeps the OWUI title/meta/manifest
intact too. The patch is idempotent and reverts cleanly on a
`pip install --upgrade open-webui`.

## Quick-start prompt suggestions

The empty new-chat screen can show 3 to 5 click-to-send buttons. They are
defined per-agent in the main `AGENTS.md` frontmatter:

```markdown
---
name: support-agent
description: Customer support for ACME.
model: openrouter/anthropic/claude-haiku-4.5
suggestions:
  - How do I check my order status?
  - What are your store hours?
  - I want to return an item
---
```

Suggestions are part of the agent's identity, not an environment knob.
They do not change between dev and prod. They are not configurable from
`.env`.

## Strip flags (the 24 defaults)

Hubzoid passes ~24 env vars to Open WebUI to strip features that do not
belong in a customer-facing single-product surface. Every flag is
overridable from `.env`. Add a line like `ENABLE_CODE_INTERPRETER=True`
to flip one back on.

### Off by default (16 flags)

| Flag | What turning it on does |
|---|---|
| `ENABLE_COMMUNITY_SHARING` | Adds a "Share to Open WebUI Community" button under every reply. |
| `ENABLE_DIRECT_CONNECTIONS` | Lets users plug in their own provider API keys, bypassing the hub. |
| `ENABLE_EVALUATION_ARENA_MODELS` | Multi-model A/B comparison UI. |
| `ENABLE_NOTES` | A parallel personal-notes product inside the chat app. |
| `ENABLE_CHANNELS` | Slack-style channels. |
| `ENABLE_CODE_INTERPRETER` | Open WebUI's own Python sandbox. **Bypasses hubzoid's tool model: no audit, no whitelist.** Add a `tools_local/` Python tool instead. |
| `ENABLE_IMAGE_GENERATION` | Inline image generation. Requires a separate image-gen account. |
| `ENABLE_RAG_WEB_SEARCH` | Open WebUI's web search. Hubzoid already has `web_search`. |
| `ENABLE_USER_WEBHOOKS` | Per-user outbound webhooks. |
| `ENABLE_TAGS_GENERATION` | Auto-tags chats with an extra LLM call. |
| `ENABLE_API_KEY` | Per-user API keys (defeats auth: a user can export hub access into a script). |
| `ENABLE_VERSION_UPDATE_CHECK` | Phones home to check for OWUI updates. |
| `ENABLE_MEMORY` | OWUI's user-memory feature. Conflicts with hubzoid's per-session memory tools. Will revisit when hubzoid ships per-user memory. |
| `ENABLE_OLLAMA_API` | Proxies Ollama. Hubzoid does not. |
| `SHOW_ADMIN_DETAILS` | Shows admin emails to regular users. |
| `ENABLE_PERSISTENT_CONFIG` | **Critical, do not flip.** When true, OWUI pins settings to SQLite on first boot and ignores env-var changes forever. Breaks hubzoid's entire env-var-as-source-of-truth model. |

### Slim runtime (3 flags)

Open WebUI loads a local ~500MB embedding model (`all-MiniLM-L6-v2`) at
startup for its RAG features. hubzoid strips OWUI's RAG entirely and reads
documents itself, so the embedder is dead weight. These defaults stop it
loading — and because hubzoid never triggers an OWUI RAG operation, the
external engine is never actually called, so no key or service is needed.

| Flag | Default | Why |
|---|---|---|
| `RAG_EMBEDDING_ENGINE` | `openai` | Any non-empty value makes OWUI skip the local embedding-model load (~500MB RAM saved). hubzoid never calls it. |
| `OFFLINE_MODE` | `True` | Don't phone HuggingFace for model updates at boot. |
| `AUDIO_STT_ENGINE` | `webapi` | Browser-side speech-to-text — 0 server RAM. |

### Workspace permissions off for non-admins (5 flags)

Admin users still see all tabs. These four hide them from regular users.

- `USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS`
- `USER_PERMISSIONS_WORKSPACE_TOOLS_ACCESS`
- `USER_PERMISSIONS_WORKSPACE_FUNCTIONS_ACCESS`
- `USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS`
- `USER_PERMISSIONS_WORKSPACE_PROMPTS_ACCESS`

### On by default (4 flags)

| Flag | Why kept on |
|---|---|
| `ENABLE_MESSAGE_RATING` | Thumbs up/down. Useful feedback for hub authors. |
| `ENABLE_TITLE_GENERATION` | Auto chat titles. One extra LLM call per new chat, worth it for history navigation. |
| `ENABLE_ADMIN_EXPORT` | Admin can export chat history. Useful for compliance and training data. |
| `ENABLE_FOLLOW_UP_GENERATION` | After each reply, OWUI suggests 2 or 3 follow-up prompts. Discovery aid for novice users. Costs one extra LLM call per turn. Operator can flip off if cost-sensitive. |

## Audio and voice

Browser-side audio (Web Speech API) is on by default: TTS read-aloud, STT
mic input, full-duplex voice mode. Zero config, no key, no cost.

Server-side audio (OpenAI / Azure / Whisper) is off. Per-hub opt-in via
`AUDIO_*` env vars when keys are available.

## File uploads

Upload UI is on. Open WebUI's server-side RAG indexing of uploaded files
is left at OWUI's defaults for now. Will revisit after the first
customer deployment with real upload patterns.
