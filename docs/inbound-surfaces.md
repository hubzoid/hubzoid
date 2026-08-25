# Inbound surfaces — WhatsApp, Telegram & generic webhooks

Surfaces that reach a hub the same way Slack and the web already do. WhatsApp and
Telegram are two-way **chat** surfaces (a person writes, the agent answers); the
**generic webhook** surface receives machine-to-hub events (alerting, CI,
automations). All are **opt-in** and **non-breaking**: with no flags and no
`identity/` folder, nothing new runs and existing hubs are unaffected.

The one genuinely new thing these need is a **public inbox** (a webhook) — Meta
can't dial out, we run Telegram as a webhook too, and machine senders POST
directly. Everything after the inbox reuses the hub's existing brain
(`/v1/chat/completions`), identity, and access guard.

## Public path — namespaced per hub

Every inbound route lives under **`/webhooks/<hub-slug>/<surface>`**, e.g.
`/webhooks/nurturing/whatsapp`, `/webhooks/irs/squadcast`. The `<hub-slug>` is the
hub's folder name (URL-slugified), or `HUBZOID_HUB_SLUG` if you pin one.

Namespacing by hub is what lets **one public front door serve many inbound hubs
at once** — the gateway edge routes `/webhooks/<slug>` to the owning hub's inbound
server. (Before 0.9.1 the path was a single global `/webhooks`, so only the first
inbound hub on a box could be reached; every other inbound hub 404'd. That limit
is gone.) The slug the inbound server derives and the slug the gateway edge routes
must match — they apply the same rule, so distinct folder names need no config.
**Set `HUBZOID_HUB_SLUG` only if the gateway had to de-dup two hubs with the same
folder basename** (the gateway raises on an inbound-port collision, so you will
know).

## The shape: one harness, many plugins

A shared harness (`hubzoid/inbound/`) owns the common plumbing — the public
`/webhooks/<hub>/<surface>` routes, raw-body handling, fast-ack, dedup, the roster
gate, conversation history, and dispatch. Each **chat** surface is a small plugin
providing four functions: **verify · parse · render · send**. The **generic
webhook** surface is simpler still (**verify · sink** — no roster, no LLM, no
reply). Identity, history, access, and analytics are shared. Gmail is designed to
slot in later as another plugin.

Per message: verify authenticity → drop duplicates → resolve the sender against
the roster (the allowlist) → load recent history → dispatch to the bridge with
the full array → render for the surface → send → record the exchange. Unknown
senders never reach the LLM. Work runs in a background task so we ack fast
(Meta/Telegram redeliver on a slow ack; dedup absorbs the repeat).

## Identity — the `identity/` folder

Presence-activated, exactly like `restricted/`. Drop **one** of these in the hub:

### `identity/access.csv` — a table you edit
```
phone,email,groups,center
919800000001,ravi@isha.org,coordinator,adyar
919800000002,priya@isha.org,coordinator,bangalore
```
- `phone` — required, any format (`+91 98000-00001` works; normalized to digits).
- `email` — the identity key (lowercased to match Open WebUI).
- `groups` — optional, `;`-separated; drives permissions.
- any other column (`center`, `name`, …) — preserved as context.
- Headers case-insensitive + trimmed; blank rows skipped; unknown phone → no access.

### `identity/access.py` — a function (for a live CRM / API lookup)
```python
def resolve(surface, handle):
    """handle is the phone. Return {"email": ..., "groups": [...]} or None."""
    row = my_crm.lookup(phone=handle)
    return {"email": row.email, "groups": row.roles} if row else None
```
Same record as a CSV row; `None` → fail-closed. Wins over the CSV if both exist.

**No `identity/` folder → every webhook sender is unknown → rejected.** The roster
is the allowlist.

**Group merge:** an identity's groups are the **union** of its Open WebUI groups
and the resolver's groups. Keep groups in OWUI (table `phone,email` only) *and/or*
in the table. Identity resolution is a **Hubzoid Enterprise** feature (free for
dev; a notice logs when unlicensed — it never blocks).

## Access control

- The channel is publicly reachable (unlike Slack). Arriving ≠ getting anything:
  an unknown sender gets a canned "not registered" reply with **no LLM, no tools,
  no data**.
- Restricted tools stay **off** on these surfaces by default (fail-closed). To let
  verified coordinators reach restricted data, add the surface to
  `HUBZOID_RESTRICTED_SURFACES` (e.g. `owui,web,api,mcp,telegram,whatsapp`) and
  give them the matching group. **Note:** the access guard runs in the *bridge*
  process, so this env change takes effect on a bridge restart (`hubzoid run`
  starts bridge + inbound together, so they never drift).

## Conversation history (memory)

Slack/OWUI give the agent memory by sending the whole conversation array each turn
(Slack replays its thread, OWUI reads its DB). Telegram/WhatsApp don't replay the
thread, so the harness keeps recent turns per `chat_id` and sends them — same
mechanism, we hold the thread.

- Stored in the **hub-owned database** (`hubzoid/db.py`), table `hz_inbound_history`
  (namespaced `hz_`; we never touch OWUI's schema).
- **SQLite by default** at `<hub>/.hubzoid/hub.db` (zero config). Set
  `DATABASE_URL=postgresql://…` for a separately-hosted Postgres — the same
  instance you can point Open WebUI at, so there is one database for the hub.
- **Bounded:** last `INBOUND_HISTORY_MAX` messages (default 40 ≈ 20 turns), so a
  chat running for months has the same footprint as a fresh one. Optional
  `INBOUND_HISTORY_TTL_DAYS` drops stale turns.
- **Isolated per `chat_id`** (a PII guarantee) — verified: two senders never see
  each other's history.
- **One database per hub.** Don't point multiple hubs at the same `DATABASE_URL`:
  history keys on `chat_id` only (no hub component), so separate hubs on one DB
  would share rows. Dedup markers under `.inbound/dedup` are not yet auto-pruned
  (small files; a periodic cleanup can be added).

## Attachments (images & files)

Media a user sends — a WhatsApp image or document, a Telegram photo, voice note,
or document — rides the **same pipeline the web and Slack already use**. The
harness downloads the bytes from the surface, POSTs them to the bridge's per-chat
uploads route (`/uploads/{chat_id}/{filename}`), and stitches a text reference
onto the user's turn:

- **Images** get an `[Image: <name>]` reference that `vision_inject` expands into
  a real image block at model-call time — the model **sees** the image directly.
- **Other files** (PDF, CSV, docs) land in the chat's uploads dir with a
  `read_upload('<name>')` note. The agent reads them on demand **if the hub
  exposes a file-read tool** — same as Slack/OWUI today.
- **Voice/audio notes** are captured as files and referenced, but are only
  *understood* if the hub provides a transcription tool (there is no native audio
  vision). Capture-and-reference works out of the box; transcription is a hub
  tool choice.

Any caption travels as the message text alongside the marker. The marker is
stored in history too, so an uploaded image stays visible for follow-up turns.
Per-file size is bounded by `HUBZOID_MAX_UPLOAD_BYTES` (default 25 MiB); an
oversized or failed download is skipped, never fatal to the turn. This is
**inbound** only (user → hub), matching Slack; sending files back is not part of
this build.

## WhatsApp setup

`.env`:
```
WHATSAPP_VERIFY_TOKEN=<any string you choose; for the GET handshake>
WHATSAPP_APP_SECRET=<Meta app secret (App settings → Basic); HMACs every POST>
WHATSAPP_TOKEN=<Graph API access token>
WHATSAPP_PHONE_NUMBER_ID=<from WhatsApp API Setup / dev console>
```
Meta configuration (dashboard, or via Graph API):
1. App webhook subscription: `POST /{app-id}/subscriptions` with
   `object=whatsapp_business_account`, `callback_url=https://<host>/webhooks/<hub>/whatsapp`,
   `verify_token=<WHATSAPP_VERIFY_TOKEN>`, `fields=messages` (app access token
   `{app-id}|{app-secret}`). Meta does the GET handshake against the callback.
2. Subscribe the WABA to the app: `POST /{waba-id}/subscribed_apps` (user token).
   Without this, inbound messages do not route. Find the WABA id via
   `GET /debug_token` granular scopes.

Behaviour:
- Every POST is verified with `X-Hub-Signature-256` (HMAC-SHA256 of the raw body).
  Duplicate deliveries dropped by message id.
- Proactive (business-initiated) messages must be an approved **template**; free
  text only inside the **24-hour** window after they reply. `send_text` /
  `send_template` cover both.
- **In-progress cue:** incoming messages are marked read (blue ticks) with a
  **typing indicator** (`mark_read` + `typing_indicator`; lasts ~25s or until the
  reply). WhatsApp has **no message editing** (so no streaming) and **no
  presence** (no "online") — these are platform limits.
- Render: strip thinking/tool tags, `*bold*`/`_italic_`, flatten tables/headings/
  links, cap 4096.

## Telegram setup

`.env`:
```
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_WEBHOOK_SECRET=<any string; Telegram echoes it in a header>
```
Register the webhook once:
`https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<host>/webhooks/<hub>/telegram&secret_token=<TELEGRAM_WEBHOOK_SECRET>`
Every POST is verified against `X-Telegram-Bot-Api-Secret-Token`.

### Enrollment (contact-share) — handled in code, no LLM
Telegram never reveals a phone on a normal message, so a coordinator binds their
numeric id to their roster row once:
1. `/start` → the bot replies with a one-tap **"Share my number"** button.
2. They tap it → the bot matches the shared phone to `identity/access.csv` and
   stores the numeric id. Only the sender's **own** contact is accepted.
3. From then on, messages from that id resolve via the stored phone.

Telegram can't cold-message a user first, so seed that first tap by distributing
the bot link over WhatsApp/email.

Behaviour:
- **Edit-streaming:** the reply fills in character-by-character via
  `editMessageText` (Telegram has no token stream). Throttled to
  `INBOUND_STREAM_INTERVAL` seconds (default 1.0; Telegram rate-limits same-message
  edits, ~0.8s floor). Set `TELEGRAM_STREAM=false` to disable (send-once).
- **Typing indicator** stays on during the reply (`sendChatAction`, re-asserted
  after the placeholder and on each edit).
- Render: strip thinking/tool tags, Telegram HTML (`<b>`, `<i>`, `<a href>`,
  `<pre>`, links kept), cap 4096.

Handshake/gate messages are **English only** for now; overridable per hub (below).

## Generic webhook setup (alerting, CI, automations)

A machine-to-hub inbox for anything that speaks HTTP POST — Squadcast/PagerDuty
downtime, a CI result, an Odoo automation, a form backend. There is **no sender to
resolve and no reply to render**, so this surface never touches the LLM. It does
exactly two things: prove the request is authentic, then hand the payload to a
**sink**.

`.env`:
```
WEBHOOK_INBOUND_SECRET=<shared secret; required — the surface is off without it>
WEBHOOK_INBOUND_NAME=squadcast   # optional; the path segment. Default: webhook
WEBHOOK_INBOUND_HMAC=false       # optional; see auth below. Default: false
```
The endpoint is then `https://<host>/webhooks/<hub>/<WEBHOOK_INBOUND_NAME>`, e.g.
`https://ishahub.isha.in/webhooks/nurturing/squadcast`.

**Auth — two modes:**
- **Shared secret (default).** Send the secret any one of three ways:
  `Authorization: Bearer <secret>`, `X-Webhook-Secret: <secret>`, or `?token=<secret>`
  in the URL. Compared in constant time; a mismatch is `403` before the sink runs.
- **HMAC (`WEBHOOK_INBOUND_HMAC=true`).** The sender signs the raw body:
  `X-Signature-256: sha256=<HMAC-SHA256(secret, body)>` — the scheme Squadcast and
  GitHub use. Verified against the exact bytes received.

**What happens to a verified event — the default sink.** The payload is written as
one JSON file per delivery under `<hub>/.inbound/webhooks/<name>/` (sortable name,
collision-free). A `schedule/*.md` task or a hub tool then reads that inbox and
decides what to do — notify a coordinator, open a ticket, page someone. This keeps
the surface unopinionated: **hubzoid owns receiving and proving; the hub owns
acting.** The stored event is `{surface, name, received_at, query, content_type,
body}`, where `body` is the parsed JSON (or the raw text if it is not JSON).

A non-JSON body is kept as text, never rejected. A sink failure returns `500` so
the provider retries; everything else acks `200 ok` fast.

## Running

```
hubzoid run <hub> --whatsapp --telegram --webhook   # combine with --slack + web UI
```
One shared inbound child on a loopback port; the public edge adds `/webhooks/<hub>`.
Standalone / systemd (serves whichever surfaces the `.env` configures):
```
hubzoid inbound run <hub>
hubzoid inbound systemd <hub> > /etc/systemd/system/hubzoid-inbound@<hub>.service
```
`HUBZOID_INBOUND_PORT` (default 8100) sets the loopback port. **Give each inbound
hub behind one gateway a distinct `HUBZOID_INBOUND_PORT`** — the gateway refuses to
plan two inbound hubs on the same port.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | SQLite `<hub>/.hubzoid/hub.db` | hub database (history); `postgresql://…` for Postgres |
| `INBOUND_HISTORY_MAX` | 40 | messages kept/sent per chat |
| `INBOUND_HISTORY_TTL_DAYS` | off | drop history older than N days |
| `INBOUND_STREAM_INTERVAL` | 1.0 | Telegram edit throttle seconds (floor 0.8) |
| `TELEGRAM_STREAM` | true | Telegram edit-streaming on/off |
| `HUBZOID_INBOUND_PORT` | 8100 | loopback port for the inbound server (unique per inbound hub under a gateway) |
| `HUBZOID_HUB_SLUG` | folder name | pins the `/webhooks/<slug>` namespace; set only to resolve a gateway slug collision |
| `WEBHOOK_INBOUND_SECRET` | — | shared secret; enables the generic webhook surface |
| `WEBHOOK_INBOUND_NAME` | `webhook` | generic webhook path segment (`/webhooks/<hub>/<name>`) |
| `WEBHOOK_INBOUND_HMAC` | false | verify `X-Signature-256` HMAC instead of a shared secret |
| `HUBZOID_RESTRICTED_SURFACES` | `owui,web,api,mcp` | add `whatsapp`/`telegram` to allow restricted tools |
| `INBOUND_MSG_VERIFY_PROMPT` / `_VERIFIED` / `_NOT_REGISTERED` / `_NOT_OWN_CONTACT` / `_PLEASE_VERIFY` / `_NO_RESPONSE` | built-in English | override the fixed handshake/gate/fallback messages |
| `HUBZOID_MAX_UPLOAD_BYTES` | 25 MiB | per-attachment ingress cap (shared with web/Slack uploads) |
| `WHATSAPP_*` / `TELEGRAM_*` | — | surface credentials (above) |

## Analytics

No new code: the surface forwards the resolved user as `X-OpenWebUI-User-Email`,
the same header the web and Slack use, so with `HUBZOID_OTEL_NORMALIZE=true` the
bridge stamps it as the span's `user.id`. Langfuse then slices usage/cost per
coordinator — no transcript storage. Self-host the OTel backend and use a
pseudonymous email in the roster if the raw identifier is sensitive.

## The daily trigger (proactive first message)

Not part of this build. It's ordinary hub-author code: a `schedule/*.md` `run:`
script that loops over coordinators and calls `send_template` (WhatsApp) or
`send_message` (Telegram) once each, optionally calling the hub's own
`/v1/chat/completions` to draft.

## Later (backlog)

- **Gmail** — another chat plugin on this harness; identity is trivial (the handle
  *is* the email). Needs Google OAuth + Pub/Sub-or-poll + email rendering.
- **Phase-2 authz** — wire the same `resolve()` into `_derive_identity` for *all*
  surfaces, so an org can manage auth via its own API instead of Open WebUI.
  Non-breaking.
