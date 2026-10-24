# Channels

A channel lets people start work from a Slack thread, a GitHub issue, a Linear
agent session, an email or a WhatsApp chat, and get the answer back in that
same place. Each provider has its own page:
[Slack](channels/slack.md), [GitHub](channels/github.md),
[Linear](channels/linear.md), [email](channels/email.md) and
[WhatsApp](channels/whatsapp.md). This page covers what they share.

Every channel turns on by a file in the hub's `channels/` folder, never a flag,
and runs with its hub: restarting the hub restarts its channels.

## The 1.x Slack app, WhatsApp, Telegram and webhooks

The earlier Slack assistant app (Socket Mode, people found by their Slack
profile email) and the inbound server for WhatsApp, Telegram and generic
webhooks turn on with a short file each; their settings stay in the hub's
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

A file whose settings are missing stays off and the start says which are
missing. For this release a hub whose `.env` alone has those settings still
starts the channel, with a notice naming the file to add. The rest of this
page is about the connections below.

## One file per connection

A connection is a file in the hub folder, `channels/<id>.yaml`:

```yaml
version: 1
id: support-slack
provider: slack
enabled: false
agent: main
queue_limit: 100
subscriptions: on_accept
progress: true
provider_config:
  # the provider's own settings, see its page
  bot_token_ref: secret:channel/support-hub/support-slack/bot-token
```

| Key | Meaning |
|---|---|
| `version` | Always `1`. |
| `id` | Lowercase letters, digits, `-` and `_`. Never changes. |
| `provider` | `slack`, `github`, `linear`, `email` or `whatsapp_cloud`. Never changes. |
| `enabled` | `false` records deliveries but starts no work. Default `false`. |
| `agent` | Answer with the hub's agent (`main`). Each person gets their own conversation, so every reply uses their own access. |
| `workflow` | Instead of `agent`: start this workflow as a work item for each request. The workflow needs `work_items=True`. |
| `run_as` | `linked_requester` (the person who asked, through their account link) for agents. `workflow` for workflows started by an unattended trigger. |
| `allowed_groups` | No longer read. Who may start work from the connection is the **Start work from &lt;id&gt;** grant (`channel_<id>`) in Settings → Access, checked with access to the agent. Nobody granted means nobody can start work. |
| `queue_limit` | Requests waiting in one thread before new ones are refused (1 to 1000, default 100). Nothing already accepted is dropped. |
| `subscriptions` | `on_accept`: once someone asks in a thread, their follow-ups continue the work without a new mention. `explicit`: every message needs a mention. |
| `progress` | Post progress updates from workflows. Requests, errors and results are always posted. |
| `provider_config` | The provider's settings. Every credential is a reference ending in `_ref`. |

Credentials are references, never values: `secret:channel/<hub>/<id>/<name>`
(set with `hubzoid secrets set <hub> --ref <reference> --stdin --apply`) or
`env:NAME`. Validation refuses a value pasted into the file.

## Setting it up

These commands work the same from a terminal or from Claude Code or Codex:

```sh
hubzoid channels validate ./my-hub --file channels/support-slack.yaml
hubzoid channels apply ./my-hub --file channels/support-slack.yaml --expected-revision absent
hubzoid channels doctor ./my-hub support-slack
hubzoid channels link ./my-hub support-slack --external-id U0123ABCD --account ana@example.com
```

`validate` makes no network call. The file is the connection's only
configuration: the hub reads it at start and when it changes (`hubzoid reload
<hub>` to be sure), and an edit that breaks it keeps the last good one while the
hub runs. `apply` checks a file and writes it for you; it names the revision it
replaces (`--expected-revision 1`), so two people never overwrite each other. `doctor`
only reads: it never posts a message, and until the provider has delivered
something it says `webhook_delivery: unverified`.

Webhook providers deliver to
`$HUBZOID_PUBLIC_URL/api/hubs/<hub>/channels/<id>/webhook`. Set
`HUBZOID_PUBLIC_URL` to the deployment's HTTPS address first.

Settings has the same controls under **Agents → agent → Configuration** (changing them there is Beta and off by default, see [DEPLOYING](DEPLOYING.md#in-app-editing)):
connections, checks, threads, deliveries, replies and account links.

## Who may ask

Every delivery is checked against the provider's signature on its exact
bytes. That proves where it came from, not who may act. Then:

1. The sender's provider account must be **linked** to a Hubzoid account by an
   administrator (`hubzoid channels link`, or **Account links** in the
   Settings). Names and email addresses in a message never count.
2. That account must be active and have access to the hub.
3. For an agent connection, it must hold the connection's grant
   (`channel_<id>`, **Start work from &lt;id&gt;** in Settings → Access).

Anyone else gets one short notice in the thread and no model is called.

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

## Replies that may not have arrived

Every reply is recorded before it is sent. If the network drops while posting,
the hub cannot know whether the provider received it. That reply is marked
**Outcome unknown** and is never sent again on its own. In Settings under
**Replies**, or with `hubzoid channels reconcile`:

- **Check thread** looks for the reply's marker. Only a find settles it.
  Not finding it is not proof that it was never posted.
- **I saw it** records that you found it yourself.
- **Retry** sends it again, accepting that it may appear twice.
- **Give up** leaves it unsent.

## Pausing

Set `enabled: false` and apply it (`hubzoid channels pause <hub> <id>
--expected-revision N`). New deliveries are still verified and recorded, but
start no work. Work that already started finishes and still replies.
`resume` reverses it. Run `doctor` first.

## Workflows that answer in a thread

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

`hub.thread.post` has no destination parameter: it always posts to the thread
the work came from. Posting the same `key` again sends nothing new. A reply
that reaches a customer names its action (`actions="customer_reply.send"`),
so the hub's write mode in `work-items.yaml` applies (`off`, `draft` or
`approve`). When the workflow returns without posting, the channel posts its
result (a string, or `reply`, `summary` or `text` from a dict) or `Done`.

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

Every command takes `--json`. Errors are `{"detail": {"code", "message", ...}}`.
Exit codes: 0 done, 1 failed, 2 invalid input, 3 permission denied, 4 conflict,
5 a dependency is unavailable.

## Writing an adapter

A provider adapter subclasses `hubzoid.channels.protocol.Adapter` and
implements `verify` (check the signature on the raw bytes), `parse` (normalize
events) and `reply` (post one part to a stored origin). `find` lets an uncertain
post be checked by its marker. The shared core handles storage,
deduplication, identity, routing, the reply record and recovery. See
`tests/channel_fake.py` for a complete minimal adapter.

## Not yet

Answering a numbered question or approving a plan from inside a thread waits
for the shared approvals and plan review services. Until then, a follow-up
from the person who asked continues the work, and a consequential send waits
for a person in the agent's **Work** tab in Settings.
