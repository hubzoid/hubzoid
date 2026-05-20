# Slack chat surface

Run your hub as a Slack bot. Users `@mention` the bot in any channel, DM it,
or chat with it from Slack's AI-assistant sidebar — same agent, same skills,
same knowledge, same `.env`. The bot connects to Slack via **Socket Mode**,
so it does not need a public URL.

```
Slack workspace
   │  (outbound WebSocket — no inbound ports needed)
   ▼
hubzoid slack run <hub>  ─── HTTP ──►  bridge on 127.0.0.1:8000
                                       (your existing `hubzoid run`)
```

The adapter is a thin client of the existing OpenAI-compatible bridge.
Anything that works in Open WebUI works here.

---

## Quick start

```bash
# 1. Run the bridge as usual.
hubzoid run my-hub

# 2. In a second terminal, generate the Slack App Manifest (JSON by default).
hubzoid slack manifest my-hub > /tmp/manifest.json
# YAML is also supported via `--format yaml` if you prefer it.
```

Open https://api.slack.com/apps → **Create New App** → **From a manifest** →
pick the workspace → paste the contents of `/tmp/manifest.json`. Slack's UI
auto-detects JSON vs YAML; the JSON tab is selected by default. Click **Next**
→ **Create**.

3. Inside the new app's settings:
   - **Install App** → **Install to Workspace**. Approve. Copy the
     **Bot User OAuth Token** (`xoxb-...`).
   - **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**.
     Name it anything; pick scope **`connections:write`**. Copy the token
     (`xapp-...`).

4. Paste both tokens into your hub's `.env`:

   ```bash
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   ```

5. Start the adapter. Two equivalent options:

   ```bash
   # Option 1 — inline with the bridge + UI (simplest for local dev).
   hubzoid run my-hub --slack
   # If the tokens above are missing, you'll get a yellow warning and the
   # bridge + UI keep running. No crash, no retry.

   # Option 2 — separate process (best for production / systemd units).
   hubzoid run my-hub          # in terminal A
   hubzoid slack run my-hub    # in terminal B
   ```

   Either way you should see `→ slack starting (Socket Mode)` or
   `hubzoid slack adapter starting (hub=my-hub, bridge=…)`.

6. Test it three ways:
   - Open the bot in Slack's AI-assistant sidebar (left rail → **Apps** → your bot).
     Click a suggested prompt. The agent should reply in the sidebar thread.
   - In any channel, invite the bot (`/invite @your-bot`) and `@mention` it.
   - Send the bot a DM.

---

## What works out of the box

| Feature | How |
|---|---|
| Thread-aware chat | The adapter reads the whole Slack thread via `conversations.replies` and passes user/assistant turns to the bridge as messages. |
| Streaming responses | The adapter streams from `/v1/chat/completions` and edits the placeholder Slack message in place every ~0.75s (well inside Slack's 1/sec/channel cap). |
| Suggested prompts | Pulled from your `AGENTS.md` frontmatter `suggestions:` field — the same field that drives Open WebUI's empty-chat buttons. |
| All hub features | Skills, knowledge, sub-agents, `tools_local`, MCP/Composio — all happen inside the bridge. The Slack adapter is just a chat surface; it doesn't know about tools. |
| `claude-local` and portable keys | Works identically. The bridge speaks one wire format; the adapter doesn't care which model is below. |

---

## Auth model

Slack is your trust boundary. Anyone in the workspace who can see the bot's
channel or DM the bot can talk to the agent. There is no per-user mapping
between Slack users and Open WebUI users; they are independent surfaces.

Restrict scope by:
- Inviting the bot only to specific channels.
- Setting **Default channels** off in the Slack app config so the bot isn't
  auto-joined.
- Using Slack workspace policies (private channels, guest restrictions) to
  control reach.

If you need per-user authorization on top of Slack's own model, file an issue
— it's tracked but not in v1.

---

## Production deployment

Two equally valid shapes — pick by your operational preference:

**A. Two systemd units (recommended for prod).** Slack adapter restarts
independently from the bridge; a Slack-side crash doesn't drop UI sessions.

```bash
hubzoid slack systemd my-hub > /etc/systemd/system/hubzoid-slack@my-hub.service
systemctl daemon-reload
systemctl enable --now hubzoid-slack@my-hub.service
```

The unit `Requires=hubzoid@my-hub.service`, so it only starts once the
bridge is up. See `docs/DEPLOYING.md` for the bridge-side service.

**B. One systemd unit, inline `--slack`.** Simpler — one process tree, one
journal. Edit the `ExecStart` in `hubzoid@.service` to append `--slack`:

```ini
ExecStart=/opt/hubzoid/.venv/bin/hubzoid run %i --slack
```

A misconfigured `SLACK_*` token only logs a warning; the bridge + UI stay
up. A Slack-side crash, however, takes the whole unit down — systemd
restarts everything together.

---

## Updating an existing Slack app

If you already created the app and Hubzoid ships a manifest change (new
scope, new event, new feature), update the app in place — don't delete
and recreate. Tokens survive the update.

1. Regenerate the manifest:

   ```bash
   hubzoid slack manifest my-hub > /tmp/manifest.json
   ```

2. Open https://api.slack.com/apps → your app → left sidebar **App Manifest**.
3. Switch the tab to **JSON** (existing apps default to YAML; the toggle is
   above the editor).
4. Replace the whole contents with `/tmp/manifest.json`. Click **Save Changes**.
5. Slack will warn about scope changes and prompt **Reinstall to Workspace**.
   Click it → **Allow**.
6. Both the Bot User OAuth Token and the App-Level Token are unchanged
   after reinstall. No `.env` edits needed.
7. Restart the adapter so it picks up the new permissions:

   ```bash
   # ^C in the adapter terminal, then
   hubzoid slack run my-hub
   ```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Slack adapter cannot start: SLACK_BOT_TOKEN, SLACK_APP_TOKEN not set` | Tokens missing from `<hub>/.env`. Run `hubzoid slack manifest` and follow steps 3-4 above. |
| `SLACK_BOT_TOKEN should start with xoxb-` | You probably pasted the App-Level Token into the bot slot. Swap them. |
| Adapter connects, but `@mention` does nothing | Make sure the **Event Subscriptions** are enabled and the `app_mention` / `message.im` events are subscribed. The manifest sets these — but if you edited the app config, re-check. |
| `SlackApiError ... missing_scope, needed: 'channels:history'` (or `groups:history` / `mpim:history`) | App was installed from an older manifest that did not include per-channel-type history scopes. [Update the app's manifest](#updating-an-existing-slack-app) and **Reinstall to Workspace**. |
| `Sending messages to this app has been turned off` (DM only) | The Slack app's **Messages** tab is off. The current manifest enables it via `features.app_home.messages_tab_enabled: true`. If you installed before that landed, [update the manifest](#updating-an-existing-slack-app). Or fix it directly: app settings → **App Home** → toggle **Allow users to send Slash commands and messages from the messages tab** on. |
| Bot replies with `:warning: error: HTTPStatusError: 401` | The bridge's `BRIDGE_API_KEYS` doesn't match what the adapter sent. Both are read from `<hub>/.env`. Restart the bridge after changing it. |
| Adapter is silent for 60+ seconds, then errors | The hub's runtime is slow (cold start) or the model is timing out. Check `hubzoid run` logs. The adapter does not impose a timeout on the bridge — it inherits whatever the runtime does. |

---

## Caveats

- **`claude-local` is interactive-only.** That constraint is documented for
  WaveAssist workflows; it applies here too. If you run the Slack adapter
  against `MODEL=claude-local`, every Slack message draws from the
  Pro/Max subscription on the host machine. Use a portable key
  (`MODEL=anthropic/...` or `openrouter/...`) if you need many concurrent
  Slack users.
- **One Slack app per hub.** Each agent has its own Slack workspace install,
  same as it has its own Open WebUI user DB.
- **No file uploads from Slack into the agent yet.** Text-chat only.
  Tracked as a v0.5+ item.
