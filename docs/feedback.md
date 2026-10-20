# Feedback on answers

People rate the agent's answers: in the web chat, by asking the agent to
save a rating, in Slack, and in WhatsApp. Every rating goes to one feedback
service and shows on the agent's **Feedback** page.

A rating is a person's view of one exact answer. It never changes the agent:
it is not memory, a Hub edit, an issue, a message to anyone or an action. To
change how the agent works, edit the Hub's instructions, knowledge or skills.

## Where people rate

| Where | How | Who is recorded |
|---|---|---|
| Web chat | Thumbs up, or thumbs down with reasons and a comment, under a finished answer | The signed-in person |
| Asking in chat | "Save negative feedback on your previous answer. The price was outdated." The agent calls `record_feedback` (off by default) | The person in the chat |
| Slack | **Helpful** / **Not helpful** buttons under a finished agent answer; Not helpful opens a dialog for reasons and a comment | The linked Hubzoid account of the person who clicked |
| WhatsApp | React 👍 or 👎 to a reply, or swipe-reply to it starting with 👎 and a comment | The outside sender, shown as "WhatsApp sender ··1234", never an account |

In the web chat a rating loads with the conversation, so a reload shows it.
Click thumbs up again to remove it. Thumbs down opens the saved reasons and
comment, with **Remove rating**. A button shows as chosen only after the
server saved it. If a save fails, what you typed stays with **Try again**. If
you changed the rating in another window, the chat shows the saved one
instead of overwriting it.

Temporary conversations and answers still being written take no rating.

## Where administrators review

**Settings → Agents → the agent → Feedback** (also `/portal/#/agents/<hub>/feedback`).
It needs the `chat.feedback.read` permission on that agent, which today
comes with Manage access.

Each row shows the rating, reasons, comment, date, source (web chat, asked in
chat, Slack, WhatsApp, CLI), who rated it, the answer and a link to its
conversation or Intake item. Filter by rating, source, reason and period;
download CSV or JSON with the same filters.

Reviewing ratings is not access to what they quote. The comment, the
person's words and the answer are shown only when you could open the source
yourself: the conversation (you own it or it is shared with you), or, for
WhatsApp, Intake (`intake.read`). Otherwise the row says the comment is
hidden. Export follows the same rule. Comments are shown as plain text, and
CSV cells that start like a formula are neutralised.

## Settings

| Setting | File | Default | Effect |
|---|---|---|---|
| `feedback.enabled` | `chat/config.yaml` | `true` | Off refuses every new or changed rating on every surface and hides the thumbs. People can still remove their own. |
| `feedback.tool` | `chat/config.yaml` | `false` | Offers the `record_feedback` tool to the agent |
| `feedback.reasons` | `chat/config.yaml` | accurate, useful, missing_source, incorrect, other | The reason ids people can choose |
| `feedback: true` | `channels/<id>.yaml` | `false` | Rating controls on that channel's final replies. Slack (agent connections) and WhatsApp (`whatsapp_cloud`, workflow connections) only |

```yaml
# chat/config.yaml
feedback:
  enabled: true
  tool: true
```

When does a change apply? Turning `feedback.enabled` or `feedback.tool` off
holds from the next rating or tool call, everywhere. Turning the tool on
rebuilds the chat runtime of the server that applied the change, so the next
web or Slack turn has it (`runtime_reloaded: true` in the apply response). In
a gateway with several hub servers, restart the other servers.

## The record_feedback tool

The agent calls it only when the person explicitly asks to save a rating.
What the tool trusts comes from the chat run, never from the model:

* the author is the person the reply acts for;
* the answer is found by walking back along the branch being viewed: by
  default the previous answer, skipping replies that only confirmed a saved
  rating. It never picks another branch, another conversation or "the last
  answer" across the deployment;
* the person's words (`quote`) and any `comment` must appear in their latest
  message, so the model can't invent a rating or put words in their mouth.

It answers `saved`, `updated`, `unchanged` (a retried call), `conflict`,
`needs_target` (no such answer: the agent asks which one) or `denied`.
It is not offered over MCP.

## Slack

Set `feedback: true` in the Slack channel file. The manifest already turns on
interactivity at the channel's webhook (HTTP) or over Socket Mode. After a
completed answer, the channel posts one small message with Slack's feedback
buttons. Acknowledgements, progress and error notices get none.

A click is verified with the signing secret like any event, saved once
(Slack's retries are one rating) and checked against the post it came from.
The person who clicked must be linked to an active account with access to
the hub, as for asking the agent. They get a private confirmation; nothing is
posted in the thread. Anyone else gets a private note and nothing is saved.
Someone else in the thread can rate the same answer: each person has their
own rating.

Not supported: ratings on Slack workflow-connection replies, and Slack
Connect.

## WhatsApp

Set `feedback: true` in the `whatsapp_cloud` channel file. A workflow's final
reply (`hub.thread.post(..., kind="result")`) then ends with:

> Rate this reply: react 👍 or 👎. To add a comment, reply to this message starting with 👎.

That line is part of the text the requester approves under
`customer_reply.send`, so approval still covers the exact message. A reply
already waiting for approval keeps the text it was recorded with.

* 👍 or 👎 on that reply records the rating; removing the reaction removes it.
* A reply to it that starts with 👍 or 👎 records the rating and the rest as
  the comment.
* Anything else, including a reaction to another message, is an ordinary
  message for Intake, as before.
* Nothing is sent back, and a rating never opens the 24-hour window.
* Meta's redeliveries are one rating; a late, older reaction never replaces a
  newer one.

The sender has no Hubzoid account and gets none: the rating's author is an
opaque id for that sender and number, never the workflow's owner or the
person who released the message.

Not supported: the older `hubzoid inbound` WhatsApp listener, Telegram,
email, Linear and GitHub.

## CLI and API

```bash
hubzoid chat feedback put <hub> --conversation <id> --message <id> --rating negative \
  --reason missing_source --comment "Cite the policy"
hubzoid chat feedback remove <hub> --conversation <id> --message <id> --expected-revision 1
hubzoid chat feedback list <hub> --source slack --rating negative
hubzoid chat feedback export <hub> --format csv --output feedback.csv
```

| Route | What |
|---|---|
| `PUT/GET/DELETE /api/conversations/{id}/messages/{mid}/feedback` | Your rating of one answer (`expected_revision`: `"absent"` to create) |
| `GET /api/conversations/{id}/feedback` | Your ratings in a conversation |
| `GET /api/hubs/{hub}/feedback` | The review list: `rating`, `source`, `reason`, `since`, `limit`, `cursor`; returns `items`, `next_cursor`, `counts` |
| `GET /api/hubs/{hub}/feedback/export?format=csv\|json` | The same, as a file (`/api/chat/feedback[/export]` on a single hub) |

CSV keeps its earlier columns in order and adds `source`, `target_kind`,
`author_kind`, `author`, `channel_id` and `excerpt` after them.

## Retention

Removing a rating keeps a record that it existed, with no comment or reasons.
Deleting a conversation removes its ratings in the same step. A WhatsApp
rating stays with its recorded reply.

## What has been verified

Web chat, the tool, review, export and restart were checked against a real
server with the scripted test model, and the tool with a live model (see the
release notes for which runtimes). Slack and WhatsApp were checked with
signed requests against fake provider APIs. They have not yet been checked
against a real Slack workspace or a real WhatsApp number.
