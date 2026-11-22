# Slack

People mention the hub's bot in a channel, or send it a direct message, and
get the answer in that same thread. See [Channels](../channels.md) for what
every provider shares: the file, account links, pausing and uncertain replies.

## The file

```yaml
version: 1
id: slack-main
provider: slack
enabled: false
agent: main
allowed_groups: [operations]
provider_config:
  transport: socket_mode            # or http when hosted
  installation_team_id: T0123ABCD   # your workspace id
  app_id: A0123ABCD                 # your Slack app id
  bot_token_ref: secret:channel/my-hub/slack-main/bot-token
  app_token_ref: secret:channel/my-hub/slack-main/app-token          # Socket Mode
  # signing_secret_ref: secret:channel/my-hub/slack-main/signing-secret   # HTTP
  allowed_channel_ids: [C0123ABCD]  # channel ids, never names
  allow_direct_messages: true
  files_enabled: true
  max_file_bytes: 20971520
  history_limit: 50
```

| Key | Meaning |
|---|---|
| `transport` | `socket_mode` (the hub connects out, no public URL, good on a laptop) or `http` (Slack posts to the hub's public URL). |
| `installation_team_id`, `app_id` | The workspace and app this connection answers. Events for any other workspace or app are refused. |
| `bot_token_ref` | The Bot User OAuth Token (`xoxb-...`). |
| `app_token_ref` | Socket Mode only: an app-level token (`xapp-...`) with `connections:write`. |
| `signing_secret_ref` | HTTP only: the app's signing secret. Every request is checked against it, on the exact bytes, within five minutes. |
| `allowed_channel_ids` | Channels where the bot answers. Empty means direct messages only. Group DMs need their id here too. |
| `allow_direct_messages` | One-to-one messages to the bot. Default true. |
| `files_enabled`, `max_file_bytes` | Bring attachments into the conversation, from `files.slack.com` only, up to the size given (at most 20 MiB). |
| `history_limit` | When someone first asks inside an existing Slack thread, how many earlier messages to read as context (0 to 200). |

Channels shared with another company (Slack Connect) are always refused.

## Setting it up with your assistant

> Connect this hub to our Slack workspace. Use a Slack app we own, allow only
> channel C0123ABCD, and let the operations group ask. Use Socket Mode on my
> laptop. Show me the files and the permissions first.

The assistant writes `channels/slack-main.yaml` (disabled), then:

```sh
hubzoid channels validate . --file channels/slack-main.yaml
hubzoid channels apply . --file channels/slack-main.yaml --expected-revision absent
hubzoid channels slack manifest . slack-main --output slack-app-manifest.json
```

In Slack: <https://api.slack.com/apps>, **Create New App**, **From a
manifest**, paste the file, then **Install to Workspace**. Then you (not the
assistant) store the tokens:

```sh
hubzoid secrets set . --ref secret:channel/my-hub/slack-main/bot-token --stdin
hubzoid secrets set . --ref secret:channel/my-hub/slack-main/app-token --stdin
```

Invite the bot to the channel (`/invite @bot`), link each person who may ask,
and check:

```sh
hubzoid channels link . slack-main --external-id U0123ABCD --account ana@example.com
hubzoid channels doctor . slack-main --actor U0123ABCD
```

`doctor` checks the token's workspace and app, the granted permissions,
channel membership and, for Socket Mode, the app-level token. It never posts.
When it passes, set `enabled: true` and apply with `--expected-revision 1`.
The bridge picks up the change within about 30 seconds, and reconnects Socket
Mode on its own after a dropped connection or a new token.

To host it, switch `transport` to `http`, set `signing_secret_ref`, set
`HUBZOID_PUBLIC_URL`, regenerate the manifest (its request URL is
`$HUBZOID_PUBLIC_URL/api/hubs/<hub>/channels/slack-main/webhook`) and update
the app in Slack.

## What people see

- A mention in a channel starts a thread: an eyes reaction, then
  `Accepted. Request 3f2a91c0.`, then the answer in the thread.
- Follow-ups in that thread continue the same conversation without a new
  mention (`subscriptions: on_accept`).
- Someone else asking in the same thread gets their own conversation, under
  their own access, after the current request finishes.
- A person who is refused gets one short notice, once per thread, visible only
  to them in a channel (a direct message is private anyway). See
  [Notices](#notices).
- The conversation also appears in the person's chat app, marked as a Slack
  thread. Asking something there stays in the chat app.

## Notices

| When | What the person sees |
|---|---|
| Not linked | "I can't act for you here yet. Ask an administrator to link your Slack account (your Slack id is U0123ABCD)." |
| Account not active, blocked, no access, no "Start work from" grant, terms not accepted | A short notice that says which, and to ask an administrator. |
| A mention in a channel not in `allowed_channel_ids` | "I don't answer in this channel. Message me directly, or ask an administrator to add this channel." |
| A direct message while `allow_direct_messages: false` | "Direct messages to me are turned off. Mention me in a channel where I answer." |
| Too many requests waiting in the thread or on the channel | "There are too many requests waiting …" |
| Only a file, with `files_enabled: false` | "Files are turned off for me here. Tell me in words what you want done." |
| A Slack guest over the daily cap (open mode) | "You've reached today's limit for guests …" |

A notice that meets a Slack rate limit is tried again for up to an hour.
Nothing is posted for Slack Connect, other bots, messages that do not mention
the bot, or while the connection is paused. A notice in a channel is an
ephemeral message: Slack shows it to that person only, and only while they
have Slack open.

## Anyone in this Slack workspace (open mode)

Off by default. An organization administrator turns it on under the
channel's **Who can ask** tab, or:

```sh
hubzoid channels open . slack-main --on --daily-cap 20
```

People with a link, or whose confirmed Slack email matches an active account,
ask as their account. Everyone else in the workspace asks as a Slack guest:
the agent's knowledge and read-only tools, no approvals, no workflows, a daily
cap. Slack Connect people and Slack's own guest accounts stay refused. While
it is on, regenerate the manifest (it adds `users:read.email` so emails can
match) and doctor warns. See
[administration](../ADMINISTRATION.md#anyone-in-a-slack-workspace-open-mode).

Restricted tools: a person with an administrator's account link keeps today's
behaviour in channel threads (their runs use the `web` surface for the access
check). A person recognised by email under open mode uses `slack-channel` or
`slack-dm`, where restricted tools stay off by default.

## Permissions the manifest asks for

`app_mentions:read`, `channels:history`, `channels:read`, `chat:write`,
`groups:history`, `groups:read`, `im:history`, `im:read`, `mpim:history`,
`mpim:read`, `reactions:write`, `users:read`, `assistant:write`, and
`files:read` when files are enabled, and `users:read.email` only while open
mode is on. Not `chat:write.public` (the bot answers only where it was
invited).

## Moving from `hubzoid slack run`

The 1.x app (`hubzoid slack run`) sends no identity unless
`SLACK_IDENTITY_MAPPING=true`, so on a hub with sign-in every sender is
refused. It says so in plain words ("I can't act for you here yet …"), never
an HTTP error, and logs a warning at start. Moving to a channel connection
fixes it properly.

```sh
hubzoid channels slack import-legacy . --id slack-main
```

writes a disabled `channels/slack-main.yaml` that refers to your existing
`SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` (`env:` references, no value is
copied), with the workspace and app ids read from Slack. Fill in
`allowed_channel_ids`, link people, grant who may ask (a capability in
Settings; `allowed_groups` is no longer read), apply, run doctor, remove
`channels/slack-assistant.yaml` (the 1.x app), restart the hub
(`hubzoid restart <hub>`) so the 1.x app stops, then set `enabled: true` and
apply the file again (`hubzoid channels apply <hub> --file ...`). A
Socket Mode connection needs no `HUBZOID_PUBLIC_URL`, and doctor skips that
check for it. While any Slack connection file exists, the 1.x Slack app stays
off, so the same Slack app never has two listeners; `hubzoid channels list`
says so. See [two ways to connect Slack and WhatsApp](../channels.md#two-ways-to-connect-slack-and-whatsapp).

## Troubleshooting

| Code | What to do |
|---|---|
| `signature_invalid` | The signing secret is wrong or the server clock is off. Never turn verification off. |
| `installation_mismatch` | The token or event is from another workspace or app. Use the configured one, or make a new connection. |
| `missing_scope` | Regenerate the manifest, update the app in Slack and reinstall it. |
| `channel_not_joined` | Invite the bot to the channel. |
| `identity_unmapped` | Link the person: `hubzoid channels link`. |
| `credential_missing` | Fill the named secret with `hubzoid secrets set`. |
| `blocked_credentials` | Slack refused the bot token. Reinstall the app and set the token again. |
| `blocked_destination` | The thread is gone, archived, or the bot left the channel. Nothing is posted elsewhere. |

## Ratings

With `feedback: true` in the channel file, a finished agent answer gets Helpful and Not helpful buttons. A click is saved as the linked account's rating and shows on the agent's Feedback page. See [Feedback on answers](../feedback.md).
