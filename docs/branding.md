# Branding and UI configuration

Hubzoid's native chat is the default; Open WebUI is an optional, permanently
supported interface. Both use files under `<hub>/branding/`. The sections below
separate their branding settings and defaults.

## Native chat branding

For the default interface (`HUBZOID_UI=hubzoid`), set the agent's display name
in the main `AGENTS.md` frontmatter and add your assets under `<hub>/branding/`.
Filenames are case-insensitive; the first available filename in each row wins.

| Surface | Files or setting |
|---|---|
| Agent name and standalone app name | `name:` in the main `AGENTS.md` frontmatter |
| Logo and agent avatar | `logo.svg`, `logo.png`, `logo.webp`, then `favicon.svg`, `favicon.png` |
| Browser favicon | `favicon.svg`, `favicon.png`, `favicon.ico`, then `logo.svg`, `logo.png` |
| Custom stylesheet | `custom.css` |

Without a custom logo, native chat displays Hubzoid's own mark; unchanged
Hubzoid template marks are treated as the default. Native chat serves these
files directly and does not use the Open WebUI asset-patching settings below.

In a gateway, `hubzoid gateway --name` sets the shared app name. Shared branding
comes from `HUBZOID_GATEWAY_BRANDING` (a hub slug or a path containing
`branding/`), then a populated `<gateway data dir>/branding/`, then the first
hub. Each agent keeps its own name and avatar. Restart after configuration
changes and refresh the browser to check the result.

## Open WebUI branding

The remaining Open WebUI-specific settings apply with `HUBZOID_UI=openwebui`.
Open WebUI's own branding stays by default. Agent prompt suggestions apply to
both interfaces. Native chat is Hubzoid's own interface and is not affected
by this section.

Open WebUI's license (clause 4) forbids altering, removing or replacing its
branding, including its name and logo, except in deployments with 50 or fewer
end users in any rolling 30 days, or with Open WebUI's prior written
permission or an enterprise license that allows it. End users are the
individual people with direct access to the application. Hubzoid can't count
them for you, so it keeps Open WebUI's branding until you say which exception
applies:

| `HUBZOID_OWUI_REMOVE_BRANDING` | Set it when |
|---|---|
| `50-users-or-fewer` | No more than 50 people have access to this deployment in any rolling 30 days. |
| `licensed` | You hold Open WebUI's written permission or an enterprise license that allows removing its branding. |
| not set (default) | Neither applies. Open WebUI's logo, favicon, splash and name stay. |

Set it in the hub's `.env` (in gateway mode, the gateway's environment) and
restart. Any other value keeps Open WebUI's branding and logs a warning.
Check the count again as your team grows.

With the setting and at least one asset in `<hub>/branding/` (a logo, favicon
or splash, not a README), the hub is branded: each file replaces its Open
WebUI counterpart, the `(Open WebUI)` suffix is removed, and the static
surfaces below use your name. Without the setting, the chat app shows Open
WebUI's stock logo, the name reads `<your name> (Open WebUI)`, and Hubzoid
restores any Open WebUI file an earlier run replaced. If your branding files
are present but the setting is not, Hubzoid logs a warning at startup saying
so. Your own `custom.css` applies either way. Don't use it to hide Open
WebUI's branding.

`HUBZOID_KEEP_OWUI_SUFFIX=true`, the setting up to 1.3.2, still keeps Open
WebUI's branding even when `HUBZOID_OWUI_REMOVE_BRANDING` is set. It is no
longer needed for that. In gateway mode the decision follows the branding
source the gateway uses (its own `branding/` folder,
`HUBZOID_GATEWAY_BRANDING`, or the first hub's).

## Per-hub identity

Three knobs control the agent's visible identity. Two live in `.env`,
one lives in a folder.

| Surface | Source | Default |
|---|---|---|
| Top-bar name + tab title | `WEBUI_NAME` in `.env` | Cascades: `WEBUI_NAME` → `MODEL_LABEL` → main agent's `name:` from `AGENTS.md` → `"Hubzoid"` |
| Watermark on copied messages | `RESPONSE_WATERMARK` in `.env` | Hub folder name |
| Logo, favicon, splash | `<hub>/branding/` folder (see below), used only with `HUBZOID_OWUI_REMOVE_BRANDING` | Open WebUI defaults |

### Branding folder

Drop files into `<hub>/branding/`. Filenames are case-insensitive. They
replace Open WebUI's only when `HUBZOID_OWUI_REMOVE_BRANDING` is set (see
above). Open WebUI's defaults render for any slot you leave empty.

| Slot | Accepted filenames |
|---|---|
| logo | `logo.{svg,png,webp,jpg,jpeg}` |
| favicon | `favicon.{svg,ico,png}` |
| splash | `splash.{png,svg,webp,jpg,jpeg}` |

If both `logo.*` and `favicon.*` exist, favicon wins. Open WebUI uses one
mark in both top-bar and tab positions.

The demo-hub template ships sample Hubzoid SVGs. In Open WebUI mode they are
used only once `HUBZOID_OWUI_REMOVE_BRANDING` is set. Replace them with your
own, or delete them.

### Static surfaces (tab title, link previews, PWA name)

`WEBUI_NAME` only renames what the SPA re-renders in the browser at
runtime. Three surfaces are served as **static files** that ship as
"Open WebUI" and never see `WEBUI_NAME`:

- the `<title>` a browser tab shows before the app hydrates,
- the `<meta>` description/OpenGraph a link-preview crawler reads when
  someone pastes the hub URL into Slack/iMessage/etc.,
- the PWA `site.webmanifest` name.

For a branded hub (branding files plus `HUBZOID_OWUI_REMOVE_BRANDING`),
Hubzoid rewrites all three on every `hubzoid run`, using the same resolved
name as the cascade above. Otherwise Hubzoid keeps Open WebUI's originals,
restoring them if an earlier run had rewritten them. The patch is idempotent and
reverts cleanly on a `pip install --upgrade open-webui`.

## Admins and other people's chats

By default an Open WebUI admin can't open, list or export another user's
chats. To allow it, set `ENABLE_ADMIN_CHAT_ACCESS=true` (viewing) and/or
`ENABLE_ADMIN_EXPORT=true` (export) in the hub's `.env` (in gateway mode,
the gateway's environment) and restart. These are environment-only
settings, so nobody can switch them on from the admin panel.

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
| `ENABLE_WEB_SEARCH` | Open WebUI's web search. Hubzoid already has `web_search`. Up to 1.3.2 Hubzoid set `ENABLE_RAG_WEB_SEARCH`, which Open WebUI 0.11.4 does not read. Its own default is also off. |
| `ENABLE_USER_WEBHOOKS` | Per-user outbound webhooks. |
| `ENABLE_TAGS_GENERATION` | Auto-tags chats with an extra LLM call. |
| `ENABLE_API_KEYS` | Per-user API keys (defeats auth: a user can export hub access into a script). Hubzoid also sets the older name `ENABLE_API_KEY`, read only by Open WebUI 0.9.5 and earlier. |
| `ENABLE_VERSION_UPDATE_CHECK` | Phones home to check for OWUI updates. |
| `ENABLE_MEMORIES`, `ENABLE_MEMORY_SYSTEM_CONTEXT`, `ENABLE_MEMORY_BACKGROUND_REVIEW` | Open WebUI's per-user memory: saved memories, its memory tool, adding memories to the system prompt and background review. Conflicts with Hubzoid's own memory. To turn it on, set `ENABLE_MEMORIES=true`. The other two then keep Open WebUI's defaults (system context on, background review off) unless you set them. Up to 1.3.2 Hubzoid set `ENABLE_MEMORY`, which Open WebUI does not read, so memory was on. An `ENABLE_MEMORY` in your `.env` is still honoured as `ENABLE_MEMORIES`. |
| `ENABLE_OLLAMA_API` | Proxies Ollama. Hubzoid does not. |
| `ENABLE_SIGNUP`, `ENABLE_OAUTH_SIGNUP` | Public email and SSO account registration. Administrators add accounts; explicit operator opt-in remains supported. |
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

### Process memory allocator (1 flag)

Not an OWUI setting — read by the subprocess's libc at startup. OWUI runs
~70 worker threads (Starlette's threadpool plus imported torch/BLAS pools),
and glibc-malloc gives each thread its own ~64MB arena, bloating RSS to
several GB of mostly-idle space (the cause of gateway OOM kills — not a
model in memory; the embedder above is already stripped). Capping arenas
holds OWUI to a normal footprint with no correctness change and no
measurable latency cost for this I/O-bound workload. Inert no-op on
musl/non-glibc.

| Flag | Default | Why |
|---|---|---|
| `MALLOC_ARENA_MAX` | `2` | Cap per-thread glibc-malloc arenas. Prevents the ~4-5GB arena bloat that OOM-killed gateway boxes. |

### Workspace permissions off for non-admins (5 flags)

Admin users still see all tabs. These four hide them from regular users.

- `USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS`
- `USER_PERMISSIONS_WORKSPACE_TOOLS_ACCESS`
- `USER_PERMISSIONS_WORKSPACE_FUNCTIONS_ACCESS` (no effect on Open WebUI
  0.11.4, which has no such permission and lets only admins manage Functions)
- `USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS`
- `USER_PERMISSIONS_WORKSPACE_PROMPTS_ACCESS`

### Model visibility for non-admins (launch-mode specific)

`BYPASS_MODEL_ACCESS_CONTROL` is **not** one of the fixed strip flags above —
its default depends on how OWUI is launched, so it is set in `_spawn_owui`
rather than `_DEFAULT_OWUI_ENV`.

OWUI (>= 0.5; confirmed on 0.9.6) filters the chat model list for `role=user`
accounts: `routers/openai.py` `get_filtered_models` keeps only models that have
a Workspace > Models DB row the user can access. Hubzoid surfaces a hub's model
as a *connection/base* model with no such row, so without a bypass every
invited non-admin user gets an empty model selector and "Model not selected"
(admins skip the filter, so they see it fine).

| Launch mode | Default | Why |
|---|---|---|
| Single hub (`hubzoid run`) | `True` | One model per hub, nothing to scope — every user should see it. |
| Gateway (`hubzoid gateway`) | `False` | Fronts many models; per-team isolation relies on per-model Private ACLs (see [DEPLOYING.md](DEPLOYING.md)). A bypass would let every user see every team's model. |

Overridable from `.env` either way (`setdefault` semantics; the operator's
explicit value wins).

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
