# Branding and UI configuration

The Hubzoid web app's brand is published, not picked up from whatever files
sit in a folder. One brand covers sign-in, chat, shared pages and Settings.
Choosing an agent changes the agent's name, avatar and suggestions, never the
deployment's brand.

### Files

`branding.yaml` (optional, `version: 1`) lives in the deployment folder: the
hub folder for a standalone hub, or the folder that holds the gateway's
deployment manifest. Its files live under `branding/`.

```yaml
version: 1
identity:
  name: Example Operations
  short_name: Example
  description: Internal work with shared knowledge
assets:
  logo_light: branding/logo-light.svg
  logo_dark: branding/logo-dark.svg
  favicon: branding/favicon.png
copy:
  en:
    welcome_title: Your team's workspace
    welcome_body: Choose an agent to get started.
    signin_title: Welcome to Example Operations
    signin_body: Sign in with your work account.
    footer: Example Operations
    suggestions: [Summarise the materials I select]
links:
  help: https://example.invalid/help
legal:
  version: null
  require_acceptance: false
announcement: null
presentation:
  hidden_features: []
  response_watermark: null
  decorative_css: null
```

| Key | Limits |
|---|---|
| `identity.name`, `short_name`, `description` | 80, 24 and 240 characters |
| `assets.*` | `logo_light`, `logo_dark`, `wordmark`, `favicon`: SVG, PNG, WebP or JPEG. `icon_192`, `icon_512`, `touch_icon`: exact-size PNG (generated from a raster favicon or logo when absent). `splash_light`, `splash_dark`, `signin_background`: PNG, WebP or JPEG. 5 MiB each, 20 MiB in all, 4096 x 4096 at most, no animation |
| `copy.en.*` | Plain text. Titles 160, other text 2000 characters, up to 12 suggestions. English is the only language |
| `links.help`, `privacy`, `terms` | `https://` URLs or paths starting with `/` |
| `legal.version`, `require_acceptance` | `require_acceptance: true` needs a version and `links.terms`. People accept once per version |
| `announcement` | `{id, text_by_locale: {en: ...}, dismissible}`. Each person can dismiss it |
| `presentation.hidden_features` | Hides navigation items (`connections`, `schedules`, `console_link`, ...). Concealment only: the server still decides what each person may do |
| `presentation.response_watermark` | Text shown under each answer |
| `presentation.decorative_css` | A `.css` file under `branding/` for the three brand slots only (below) |

Colours, fonts and corner radius go in `branding/theme.json`, the shared theme
schema (see [chat configuration](chat-config.md#theme)).

SVG files may only draw: no scripts, event attributes, external references,
`<style>`, `style=` or `foreignObject`. Raster images are re-encoded, which
removes metadata. Unsafe files are refused with a reason, never silently
changed.

Decorative CSS may style exactly `[data-brand-slot="brand.logoFrame"]`,
`[data-brand-slot="brand.welcome"]` and `[data-brand-slot="brand.footer"]`, with
`background-color` and `border-color` (six-digit hex), `border-radius` (0 to
24px) and `padding` (0 to 32px). Approval cards, errors and permission notices
are never inside a brand slot. `custom.css` is no longer served.

### Publishing

Nothing reaches teammates until an exact candidate is reviewed and activated.

```bash
hubzoid branding inspect . --json            # source, active revision, legacy files
hubzoid branding validate . --json           # every file, every palette mode
hubzoid branding draft create . --json       # returns id, revision, digest, preview_url
hubzoid branding diff <draft> . --json
hubzoid branding review <draft> . --digest <digest> --expected-revision <draft revision> --decision approve
hubzoid branding activate <draft> . --review <review id> --expected-revision <active generation>
hubzoid branding history . --json
hubzoid branding rollback . --revision <revision id>   # a new draft: review and activate it
hubzoid branding export . --output brand-export
```

Open the `preview_url` (`/branding-preview/<draft>`) while signed in as an
administrator to see the draft on sign-in, chat, a shared page and Settings
in light, dark and OLED. Settings → Workspace settings → Brand does the same steps,
and can edit the main fields and upload a logo or favicon as a new draft. A
changed file, a changed active brand or a new validator release needs a fresh
review. Activation is atomic: a retried request with the same
`Idempotency-Key` returns the same activation, and a stale `expected-revision`
is refused.

`hubzoid branding import-legacy . --write` turns a pre-2.0 `branding/` folder
into a `branding.yaml` and reports each file: mapped, not used, or dropped
(`custom.css`).

The API is `/api/branding/...` (drafts, assets, preview, reviews, activate,
history, export, rollbacks) and the public `GET /api/branding`.

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

## Coming from Open WebUI (1.x)

Hubzoid 1.x passed Open WebUI a set of environment flags to brand it and strip
features. The web app reads none of them; `hubzoid doctor` and the start name
any it finds ([upgrading to 2.0](UPGRADING.md#upgrading-to-20-open-webui-removed)).

| 1.x setting | In the web app |
|---|---|
| `WEBUI_NAME` | `identity.name` in `branding.yaml`. Until one is published, the name comes from the gateway or the main agent, then `WEBUI_NAME` |
| `RESPONSE_WATERMARK` | `presentation.response_watermark` |
| `<hub>/branding/logo.*`, `favicon.*`, `splash.*` | `assets.*`; `hubzoid branding import-legacy` maps them |
| `HUBZOID_KEEP_OWUI_SUFFIX` | No equivalent: Open WebUI's branding terms don't apply to the web app |
| `ENABLE_*` feature flags, `DEFAULT_PROMPT_SUGGESTIONS`, `DEFAULT_MODELS`, `ENABLE_ADMIN_CHAT_ACCESS`, `ENABLE_ADMIN_EXPORT` | Chat defaults in Settings → Workspace settings → Chat defaults and `chat/config.yaml` ([chat configuration](chat-config.md)) |
| `RAG_EMBEDDING_ENGINE`, `AUDIO_STT_ENGINE`, `MALLOC_ARENA_MAX` for Open WebUI | Not needed: no Open WebUI process runs |

Voice uses the browser's own speech features, with no server key. Uploaded
files are kept per conversation and read by the agent's tools.
