# Feedback across web chat, a chat tool, Slack and WhatsApp

Status: agreed scope, implemented on `feat/feedback-across-channels`.
Builds on chat feedback (`hubzoid/chat/feedback.py`, migration `op_0025`), the
channel contract (2026-10-19-channels.md), Slack (2026-10-19-slack-channel.md)
and WhatsApp intake (2026-10-19-whatsapp-intake.md).

## Problem

A person can rate an answer in the web chat, but the controls are unfinished:
a reload forgets their rating, it cannot be changed or removed, a failed save
loses the reason and comment, and "Thanks" shows before the server agrees.
Nobody can review the ratings except through `GET /api/chat/feedback`. People
who use the agent from Slack or WhatsApp have no way to rate a reply, and a
person who says "save negative feedback on your last answer" in chat gets
nothing saved.

## What should happen

One feedback service records every rating, whatever the surface, and one
agent-level Feedback page shows them.

**Feedback is a person's assessment of one result.** It never becomes memory,
a Hub instruction, an issue, a message to anyone or a business action.

### Targets

A rating belongs to exactly one delivered result:

| Target kind | Identity | Used by |
|---|---|---|
| `message` | the native chat answer's message id (immutable) | web chat, the tool, Slack agent connections |
| `channel_reply` | the recorded outside write (`hz_operations` id) of the delivered reply | WhatsApp intake replies |

Slack agent connections already answer in the person's own native
conversation, so a Slack rating targets that answer's message id: the same
record a web rating of that answer would update. WhatsApp intake replies are
sent by a workflow and have no conversation, so they are rated by the
recorded operation that sent them. No conversation or account is invented to
fit the older schema.

### Authors

| Author kind | `author_id` | Shown as |
|---|---|---|
| `account` | the Hubzoid account id | the account's name or email (to feedback reviewers only) |
| `external` | `ext:<channel thread id>` (opaque, one per WhatsApp sender and number) | "WhatsApp sender ··1234" |

An external sender is never mapped to an account, the workflow owner or the
person who released their message. One author has at most one live rating per
target (the existing unique key).

### Sources

`web`, `tool`, `slack`, `whatsapp`, `cli`. Existing rows read as `web`.

## Configuration

| Setting | Where | Default | Effect |
|---|---|---|---|
| `feedback.enabled` | `chat/config.yaml` (existing) | `true` | Master switch. Off refuses every new or changed rating on every path (web, tool, Slack, WhatsApp, CLI). A person can still remove their own rating. |
| `feedback.tool` | `chat/config.yaml` (new) | `false` | Offers `record_feedback` to the agent. |
| `feedback: true` | `channels/<id>.yaml` (new optional field) | `false` | Shows rating controls on that channel's final replies. Accepted for `slack` and `whatsapp_cloud` only. |

No new required Hub file or field. Turning the tool off is checked on every
call, so it stops at once on every surface. Turning it on rebuilds the chat
runtime on the bridge that applied the change, so the next web or Slack turn
has the tool. A gateway deployment applies it to other bridges at restart.

## Permissions

* Rating: web and tool need a conversation the person can read (existing
  check). Slack needs a linked, active account with access to the hub, the same
  rule as asking the agent. WhatsApp needs a signed Meta delivery from the
  sender the reply was sent to.
* Reviewing: `chat.feedback.read` on that hub (existing key). A reviewer always
  sees the rating, reasons, date, source and author. The comment, tags, answer
  excerpt and source link are shown only when the reviewer could open the
  source themselves: the conversation (owner or member) for a `message`
  target, `intake.read` for a WhatsApp reply. Export applies the same rule.
  Managing feedback is not access to a private conversation or thread.
* Counts are over the reviewer's hub only. No search over comment text.

## The `record_feedback` tool

A FunctionTool in `hubzoid/tools/feedback_tool.py`, adapted by the existing
Claude and Codex adapters like every hub tool.

Arguments: `rating` (positive or negative), `quote` (the person's exact words
asking to save it), optional `comment` (the person's exact words), optional
`reason_ids`, optional `answers_back` (1 to 5, default 1).

Everything else comes from the trusted run: the run identity record
(`hz_run_identities`, written before the model ran) gives the account, the
conversation and the reply being written. The tool:

1. refuses unless `feedback.enabled` and `feedback.tool` are on now, and the
   call is inside a chat reply whose run identity is the caller's own;
2. checks that `quote` and `comment` appear in the person's latest message,
   so the model cannot invent a vote or put words in their mouth;
3. walks back along the current branch from that message to the
   `answers_back`-th finished answer, skipping replies that only confirmed a
   saved rating, never another branch, conversation or the reply in progress;
4. records it with source `tool`, the inbound message id and the quote; a
   retried call with the same arguments changes nothing.

It returns `saved`, `updated`, `unchanged`, `conflict`, `needs_target` or
`denied`, with a short reason the model can say back.

## Slack

Following eve's renderer: after an agent connection posts a completed answer,
it posts one small message with Slack's feedback buttons ("Helpful", "Not
helpful"). Acknowledgements, progress and error notices get none. The prompt
is a recorded post of its own, tied to the run.

A click arrives signed on the existing webhook (or Socket Mode), is committed
like any delivery (so a repeated delivery is one record), and is routed:
the clicking Slack user must be linked; the clicked message must be a prompt
this connection posted in that thread for that run. "Helpful" saves
immediately. "Not helpful" saves, then opens a dialog for reasons and a
comment. The person gets a private confirmation. Nothing is posted to the
thread.

## WhatsApp

Supported on the `whatsapp_cloud` intake channel only. The older
`hubzoid inbound` WhatsApp listener is not supported (it has no durable record
of each reply).

With `feedback: true`, a final reply (`hub.thread.post(kind="result")`) ends
with one line: "Rate this reply: react 👍 or 👎. To add a comment, reply to
this message starting with 👎." The line is part of the text the requester
approves, so approval still covers the exact message and nothing changes
after approval. Replies that already wait for approval keep the text they
were recorded with.

* A 👍 or 👎 reaction to that reply records a rating; removing the reaction
  removes it. Other emoji are ordinary messages, handled as today.
* A swipe-reply to that reply starting with 👍 or 👎 records the rating with
  the rest as the comment.
* A reaction or reply that points at anything else goes to Intake as today.
* Nothing is sent back, so no approval or 24-hour window applies, and a
  feedback event never opens the window.
* Meta redeliveries are one record (existing admission). Events are applied
  in provider time order, so a late, older reaction never replaces a newer one.

## Web chat

* Each person's ratings for a conversation load with it (one request), so a
  reload shows them.
* Thumbs up toggles: a second click removes it. Thumbs down opens the form
  with the saved reasons and comment, with Save and Remove.
* A button shows as selected only after the server saved it. A failed save
  keeps the form open with the draft and a Retry. A revision conflict reloads
  the saved rating, keeps the draft and asks to save again.
* Temporary conversations and unfinished answers show no controls (existing
  rules). Keyboard, touch, narrow screens and both themes are covered.

## Review page

Agents → agent → **Feedback**, in the existing agent tabs. Filters: rating,
source, reason and period. Cursor pagination, empty, loading and error
states, JSON and CSV export with the same filters. Each row: rating, reasons,
comment, date, source, author, the answer excerpt and a link to the
conversation or Intake item when the reviewer may open it. Comments are
rendered as text.

## Storage and migration

`op_0072` adds nullable columns to `hz_feedback`: `target_kind`, `source`,
`author_kind`, `author_label`, `channel_id`, `thread_id`, `request_key`, and
makes `conversation_id` nullable (batch alter, SQLite and PostgreSQL). Old rows
keep working as `message`, `web`, `account`. `op_0025` is not edited. The CSV
keeps its columns in order and adds the new ones at the end.

Retention: deleting a conversation tombstones its ratings in the same
transaction (comment, tags and reasons cleared), as it does its messages.
Removing a rating keeps a tombstone (existing behaviour). Channel-reply ratings
follow the channel's records.

## Scope and non-goals

* No issue workflow, assignment or triage. No automatic memory or Hub edit.
* No feedback on Slack workflow-connection replies, Linear, GitHub, email or
  Telegram, and no claim that they work.
* No outbound feedback prompt to a WhatsApp customer as a separate message.
* No rating aggregation dashboards beyond counts on the review page.

## Tests

Extended `tests/test_chat_feedback.py`, new `tests/test_feedback_tool.py`,
`tests/test_feedback_channels.py` (signed Slack and Meta requests, fake
providers, slow where the bridge runs), the migration test, and portal
journeys in `portal/tests/app-real-journey.cjs` (journey `feedback`).

## Definition of done

* A rating saved on the web, by the tool, in Slack and from WhatsApp appears
  once, with its source and author, on the agent's Feedback page and in export.
* Reload, change and removal work in the browser; a manager without access to
  a conversation sees the rating but not its comment or excerpt.
* Turning feedback off refuses new ratings everywhere; turning the tool off
  stops it on the next call.

## Implementation plan

1. Migration `op_0072` and the service: target, author and source; shared
   validation; `record()` for tool and channel paths; list with visibility,
   filters, excerpts and links; tombstone on conversation delete.
2. Routes: the person's ratings per conversation; hub-scoped list and export
   for the console; config apply rebuilds the runtime when `feedback` changes.
3. `record_feedback` tool and its registration.
4. Channel config field; Slack prompt, click and dialog routing; WhatsApp
   footer, reactions and quoted replies.
5. Web chat controls; console Feedback tab; built bundle.
6. Tests, docs (`docs/FEEDBACK.md`), browser and live-model runs, proof.
