# Inbound surfaces — WhatsApp & Telegram

Two-way chat surfaces that reach a hub the same way Slack and the web already do.
Both are **opt-in** and **non-breaking**: with no flags and no `identity/` folder,
nothing new runs and existing hubs are unaffected.

The one genuinely new thing WhatsApp/Telegram need is a **public inbox** (a
webhook) — Meta can't dial out, and we run Telegram as a webhook too. Everything
after the inbox reuses the hub's existing brain (`/v1/chat/completions`),
identity, and access guard.

## The shape: one harness, many plugins

A shared harness (`hubzoid/inbound/`) owns the common plumbing — the public
`/webhooks/<surface>` route, raw-body handling, fast-ack, dedup, the roster gate,
conversation history, and dispatch. Each surface is a small plugin providing four
functions: **verify · parse · render · send**. Identity, history, access, and
analytics are shared. Gmail is designed to slot in later as a third plugin.

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
   `object=whatsapp_business_account`, `callback_url=https://<host>/webhooks/whatsapp`,
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
`https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<host>/webhooks/telegram&secret_token=<TELEGRAM_WEBHOOK_SECRET>`
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

## Running

```
hubzoid run <hub> --whatsapp --telegram     # combine with --slack and the web UI
```
One shared inbound child on a loopback port; the public edge adds `/webhooks`.
Standalone / systemd:
```
hubzoid inbound run <hub>
hubzoid inbound systemd <hub> > /etc/systemd/system/hubzoid-inbound@<hub>.service
```
`HUBZOID_INBOUND_PORT` (default 8100) sets the loopback port.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | SQLite `<hub>/.hubzoid/hub.db` | hub database (history); `postgresql://…` for Postgres |
| `INBOUND_HISTORY_MAX` | 40 | messages kept/sent per chat |
| `INBOUND_HISTORY_TTL_DAYS` | off | drop history older than N days |
| `INBOUND_STREAM_INTERVAL` | 1.0 | Telegram edit throttle seconds (floor 0.8) |
| `TELEGRAM_STREAM` | true | Telegram edit-streaming on/off |
| `HUBZOID_INBOUND_PORT` | 8100 | loopback port for the inbound server |
| `HUBZOID_RESTRICTED_SURFACES` | `owui,web,api,mcp` | add `whatsapp`/`telegram` to allow restricted tools |
| `INBOUND_MSG_VERIFY_PROMPT` / `_VERIFIED` / `_NOT_REGISTERED` / `_NOT_OWN_CONTACT` / `_PLEASE_VERIFY` | built-in English | override the fixed handshake/gate messages |
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

- **Gmail** — a third plugin on this harness; identity is trivial (the handle
  *is* the email). Needs Google OAuth + Pub/Sub-or-poll + email rendering.
- **Phase-2 authz** — wire the same `resolve()` into `_derive_identity` for *all*
  surfaces, so an org can manage auth via its own API instead of Open WebUI.
  Non-breaking.
