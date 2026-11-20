# Channels

A channel lets people start work where they already are and get the answer in
that same place. Each connection is one file in the hub's `channels/` folder,
and it runs with its hub: restarting the hub restarts its channels. This page
covers what every provider shares.

| Provider | `provider` | Work comes from |
|---|---|---|
| [Slack](channels/slack.md) | `slack` | A mention or message in a Slack thread |
| [GitHub](channels/github.md) | `github` | A mention, a label, a new pull request or failed checks |
| [Linear](channels/linear.md) | `linear` | An issue delegated to the hub's app, or a mention |
| [ClickUp](channels/clickup.md) | `clickup` | A task moved to a status, or given a tag |
| [Email](channels/email.md) | `email` | New mail in a shared mailbox, started directly or after a person releases it in Intake |
| [WhatsApp](channels/whatsapp.md) | `whatsapp_cloud` | A message to your WhatsApp Business number, released by a person in Intake |

## Example

`channels/support-slack.yaml`:

```yaml
version: 1
id: support-slack
provider: slack
enabled: false
agent: main
provider_config:
  # the provider's own settings, see its page
  bot_token_ref: secret:channel/support-hub/support-slack/bot-token
```

## Settings

Every provider shares these keys. The provider's own settings go under
`provider_config`.

| Key | Default | What it does |
|---|---|---|
| `version` | required | Always `1`. |
| `id` | required | 1 to 64 lowercase letters, digits, `-` and `_`. Never changes. |
| `provider` | required | `slack`, `github`, `linear`, `clickup`, `email` or `whatsapp_cloud`. Never changes. |
| `enabled` | `false` | `false` records deliveries but starts no work. |
| `agent` | none | Answer with the hub's agent (`main`). Each person gets their own conversation, so every reply uses their own access. |
| `workflow` | none | Instead of `agent`: start this workflow as a work item for each request. The workflow needs `work_items=True`. Set exactly one of `agent` and `workflow`. |
| `run_as` | `linked_requester` for an agent, `workflow` for a workflow | `linked_requester` runs as the person who asked, through their account link. `workflow` runs unattended triggers as the workflow's own account. An agent connection always uses `linked_requester`. |
| `queue_limit` | `100` | Requests waiting in one thread before new ones are refused (1 to 1000). Nothing already accepted is dropped. |
| `subscriptions` | `on_accept` | `on_accept`: once someone asks in a thread, their follow-ups continue the work without a new mention. `explicit`: every message needs a mention. |
| `progress` | `true` | Post progress updates from workflows. Requests, errors and results are always posted. |
| `feedback` | `false` | Rating controls on final replies, where the provider supports them (Slack agent answers, WhatsApp workflow replies). |
| `allowed_groups` | `[]` | No longer read. `doctor` warns when it is set. Use the grant below. |
| `provider_config` | `{}` | The provider's settings. Every credential is a reference ending in `_ref`. |

Credentials are references, never values: `secret:channel/<hub>/<id>/<name>`
(set with `hubzoid secrets set <hub> --ref <reference> --stdin`) or
`env:NAME`. Validation refuses a value pasted into the file.

In a channel file you can leave the hub's folder name out:
`secret:channel/<id>/<name>` means this hub's secret for the channel with that
`id` (in `channels/github.yaml`, `secret:channel/github/private-key`). The file
keeps what you wrote, so it works in a clone under any folder name. Everywhere
else, such as `hubzoid secrets set --ref`, write the full form.

## Connect a channel

These commands work the same from a terminal or from Claude Code or Codex:

```sh
hubzoid channels validate ./my-hub --file channels/support-slack.yaml
hubzoid channels apply ./my-hub --file channels/support-slack.yaml --expected-revision absent
hubzoid channels doctor ./my-hub support-slack
hubzoid channels link ./my-hub support-slack --external-id U0123ABCD --account ana@example.com
```

- `validate` makes no network call.
- `apply` checks the file and writes it. It names the revision it replaces
  (`--expected-revision 1`), so two people never overwrite each other.
- `doctor` only reads. It never posts a message, and until the provider has
  delivered something it says `webhook_delivery: unverified`.
- `link` ties a provider account to a Hubzoid account.

The file is the connection's only configuration. The hub reads it at start
and when it changes (`hubzoid reload <hub>` to be sure). An edit that breaks
it keeps the last good one while the hub runs.

Webhook providers deliver to
`$HUBZOID_PUBLIC_URL/api/hubs/<hub>/channels/<id>/webhook`. Set
`HUBZOID_PUBLIC_URL` to the deployment's HTTPS address first.

Settings has the same controls under **Agents**, the agent, **Configuration**:
connections, checks, threads, deliveries, replies and account links. Changing
them there is Beta and off by default (see
[DEPLOYING](DEPLOYING.md#in-app-editing)).

## Who may ask

Every delivery is checked against the provider's signature on its exact
bytes. That proves where it came from, not who may act. Then:

1. The sender's provider account must be **linked** to a Hubzoid account by an
   administrator (`hubzoid channels link`, or **Account links** in Settings).
   Names and email addresses in a message never count.
2. That account must be active and have access to the agent.
3. It must hold the connection's grant, **Start work from &lt;id&gt;**
   (`channel_<id>`) in Settings, Access. Nobody granted means nobody can
   start work.

Anyone else gets one short notice in the thread and no model is called.

A workflow connection can also start unattended work that no linked person
asked for, when the provider vouches for it (for example a GitHub label added
by a collaborator). That work runs as the workflow's own account. Email and
WhatsApp senders are outside people, not linked accounts. See each
provider's page.

## What happens to a message

1. The delivery is verified and saved before the provider gets its answer.
   If saving fails, the provider is told to retry.
2. A repeat (the same delivery again, or the same message under another
   event type) is recorded as a duplicate and does nothing. A reused
   delivery id with different content is quarantined.
3. The request goes to the person's conversation (agent) or starts a work
   item (workflow). In one thread, one request runs at a time. A message from
   someone else waits its turn.
4. The answer is posted to the thread it came from, and nowhere else.

A restart at any point picks up where it stopped: saved deliveries are routed,
and finished answers that were not posted yet are posted once.

## Questions and approvals in the thread

| Provider | Answer a question in the thread | Approve a plan in the thread |
|---|---|---|
| ClickUp | A comment from the person who asked | With `approve_in_task: true`: `approve`, `reject` or `change: ...` |
| Linear | A prompt in the agent session from the person who asked | No |
| Other connections | No | No |

A number picks an option. A reply only answers when exactly one question
waits. Anything else is a follow-up the work reads at its next step. Every
other question or approval is settled in Hubzoid, in the Inbox, and a
consequential send waits for a person in the agent's **Work** tab in
Settings.

## Replies that may not have arrived

Every reply is recorded before it is sent. If the network drops while posting,
the hub cannot know whether the provider received it. That reply is marked
**Outcome unknown** and is never sent again on its own. In Settings under
**Replies**, or with `hubzoid channels reconcile`:

| Choice | Effect |
|---|---|
| **Check thread** | Looks for the reply's marker. Only a find settles it. Not finding it does not prove it was never posted. |
| **I saw it** | Records that you found it yourself. |
| **Retry** | Sends it again, accepting that it may appear twice. |
| **Give up** | Leaves it unsent. |

## Pause a channel

```sh
hubzoid channels pause ./my-hub support-slack --expected-revision 3
hubzoid channels doctor ./my-hub support-slack
hubzoid channels resume ./my-hub support-slack --expected-revision 4
```

`pause` sets `enabled: false`. New deliveries are still verified and
recorded, but start no work. Work that already started finishes and still
replies. Run `doctor` before `resume`.

Pause, resume, connecting and `configure` change only their own settings in
the file as it is now, so your other edits stay. If the file is not valid,
they stop and name it, except `pause`: it still works, and replaces the file
with the last applied configuration so intake stops.

## Answer from a workflow

A workflow started by a channel can post to its thread:

```python
from hubzoid import hub, workflow

@workflow(work_items=True)
def triage():
    request = hub.work_item.input          # text, files and who asked
    hub.thread.post("Looking at it now.", key="started", kind="progress")
    summary = hub.call_agent(f"Triage this request: {request['text']}")
    hub.thread.post(summary, key="summary")
    return {"posted": True}
```

- `hub.thread.post` has no destination parameter. It always posts to the
  thread the work came from. Posting the same `key` again sends nothing new.
- `kind` is `progress`, `result`, `notice`, `question` or `approval` (the work
  now waits for an approval).
- A reply that reaches a customer names its action
  (`actions="customer_reply.send"`), so the hub's write mode in
  `work-items.yaml` applies (`off`, `draft` or `approve`).
- `files=[...]` attaches files the hub wrote under its `output/` folder (at
  most five, 10 MB each) where the provider supports it
  (`hub.thread.attachments`, ClickUp today). Elsewhere the text stands alone,
  so keep a link in it.
- `hub.thread.approvals_here` says whether the requester may settle a plan
  approval by replying in the thread, so the text can say what to reply.
- When the workflow returns without posting, the channel posts its result (a
  string, or `reply`, `summary` or `text` from a dict) or `Done`.

## Commands

| Command | API |
|---|---|
| `hubzoid channels list HUB` | `GET /api/hubs/{hub}/channels` |
| `hubzoid channels validate HUB --file F` | `POST .../channels/validate` |
| `hubzoid channels apply HUB --file F --expected-revision N` | `PUT .../channels/{id}/config` |
| `hubzoid channels export HUB ID` | `GET .../channels/{id}/config` |
| `hubzoid channels pause / resume HUB ID --expected-revision N` | `POST .../channels/{id}/enabled` |
| `hubzoid channels doctor HUB ID [--actor U]` | `POST .../channels/{id}/doctor` |
| `hubzoid channels threads / inspect HUB ID [--thread T]` | `GET .../channels/{id}/threads[/{thread}]` |
| `hubzoid channels deliveries HUB ID` | `GET .../channels/{id}/deliveries` |
| `hubzoid channels redrive HUB ID DELIVERY` | `POST .../channels/{id}/deliveries/{delivery}/redrive` |
| `hubzoid channels outbox HUB ID` | `GET .../channels/{id}/outbox` |
| `hubzoid channels reconcile HUB ID OPERATION --decision check` | `POST .../channels/{id}/outbox/{operation}/resolve` |
| `hubzoid channels subscribe / unsubscribe HUB ID --thread T` | `PUT .../channels/{id}/threads/{thread}/subscription` |
| `hubzoid channels link / unlink / links HUB ID` | `POST / DELETE / GET .../channels/{id}/links` |

Providers add their own commands: `hubzoid channels github ...`,
`hubzoid channels clickup ...` and others, on each provider's page.

Every command takes `--json`. Errors are `{"detail": {"code", "message", ...}}`.
Exit codes: 0 done, 1 failed, 2 invalid input, 3 permission denied, 4 conflict,
5 a dependency is unavailable.

## Write an adapter

A provider adapter subclasses `hubzoid.channels.protocol.Adapter` and
implements `verify` (check the signature on the raw bytes), `parse` (normalize
events) and `reply` (post one part to a stored origin). `find` lets an
uncertain post be checked by its marker. The shared core handles storage,
deduplication, identity, routing, the reply record and recovery. See
`tests/channel_fake.py` for a complete minimal adapter.

## Two ways to connect Slack and WhatsApp

Slack and WhatsApp each have two setups. Both are current and supported.
The newer one is a **connection** (the rest of this page). The earlier one is
the **1.x app**.

| | Connection (`provider: slack` or `whatsapp_cloud`) | 1.x app (`provider: slack_assistant` or `whatsapp_inbound`) |
|---|---|---|
| Use it for | New setups. Threads, routing to an agent or workflow, pausing, delivery checks, a Settings page | An existing 1.x deployment that works, or a quick Socket Mode Slack bot |
| Settings | References in the file (`bot_token_ref: env:SLACK_BOT_TOKEN`) | Variables in `.env` |
| Slack transport | `socket_mode` (no public URL) or `http` (needs `HUBZOID_PUBLIC_URL`) | Socket Mode only |
| Who is who | Linked accounts (`hubzoid channels link`) | Slack profile email (`SLACK_IDENTITY_MAPPING`) |
| Shows in | `hubzoid channels list` and `doctor` | `hubzoid channels list` (as a `1.x channel` line) and the start |

**They can't run side by side on one Slack app.** A Slack connection (even a
disabled one) takes over the Slack tokens, so the 1.x Slack app stays off, and
the start and `hubzoid channels list` say so. `hubzoid run --slack` and
`--whatsapp` are gone: each setup turns on with its file.

To move a 1.x Slack app to a connection, see
[moving from `hubzoid slack run`](channels/slack.md#moving-from-hubzoid-slack-run).

### The 1.x Slack app, WhatsApp, Telegram and webhooks

The earlier Slack assistant app (Socket Mode, people found by their Slack
profile email) and the inbound server for WhatsApp, Telegram and generic
webhooks turn on with a short file each. Their settings stay in the hub's
`.env`:

```yaml
# channels/slack-assistant.yaml
version: 1
id: slack-assistant
provider: slack_assistant   # or whatsapp_inbound, telegram, webhook
enabled: true
```

| `provider` | Its settings in `.env` | Arrives at |
|---|---|---|
| `slack_assistant` | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | Socket Mode (no public URL) |
| `whatsapp_inbound` | `WHATSAPP_*` | `/webhooks/<hub>/whatsapp` |
| `telegram` | `TELEGRAM_*` | `/webhooks/<hub>/telegram` |
| `webhook` | `WEBHOOK_INBOUND_*` | `/webhooks/<hub>/<name>` |

A file whose settings are missing stays off, and the start says which are
missing. For this release a hub whose `.env` alone has those settings still
starts the channel, with a notice naming the file to add.

## Troubleshooting

| Symptom | Next step |
|---|---|
| `channels doctor` fails `public_url_required` | Set `HUBZOID_PUBLIC_URL` to an HTTPS address. A Slack connection on `socket_mode` no longer needs it (fixed in 2.0.9). |
| `doctor` warns that `allowed_groups` is no longer read | Remove it and grant **Start work from &lt;id&gt;** in Settings. |
| Someone gets "Ask an administrator to link your ... account" | `hubzoid channels link` their provider account. |
| The 1.x Slack app stopped after adding a Slack connection | Expected: one Slack app, one setup. Remove the connection file to go back. |
| `hubzoid channels list` says "No channels." while a bot answers | Upgrade to 2.0.9: 1.x channels are listed too. |
| `hubzoid run --slack` stops with exit code 2 | Add `channels/slack-assistant.yaml` (shown above) instead of the flag. |
| A WhatsApp message gets no answer | `hubzoid channels doctor <hub> <id>` for a connection. For the 1.x app, check `WHATSAPP_*` in `.env` and the `/webhooks/<hub>/whatsapp` URL in Meta. |
| A reply shows **Outcome unknown** | See [Replies that may not have arrived](#replies-that-may-not-have-arrived). |
