# Email intake on the shared channel contract

Status: proposed for 2.0. Builds on the channel contract
(2026-10-19-channels.md) and recorded writes with write modes.

## Problem

Orders and requests arrive by email, often with attachments. A hub cannot
read a shared mailbox today, so people copy messages into chat by hand, and
nothing records who sent what or whether a reply went out.

## What should happen

- A connection reads one shared mailbox over IMAP (with SMTP to send), the
  Gmail API or Microsoft 365, using the customer's own credentials or OAuth
  app. Only mail that arrives after it starts is read, and nothing in the
  mailbox is changed.
- Each message is kept with its source and attachments. A sender on the
  exact allowlist with a valid signature from their own domain starts the
  configured workflow, running as the workflow. Everyone else waits in a new
  Intake list with the reason, where a person releases or rejects it.
- Replies are recorded writes under the hub's `email.send` mode: off, a real
  draft in the mailbox (after a one-time check that drafts work), or sent
  after the requester approves the exact message. They go only to the
  sender, in the same conversation, and an uncertain send is never repeated
  on its own.

## Scope and non-goals

- No automatic sending mode.
- A follow-up question to the sender and its answer are ordinary intake and
  ordinary approved sends; there is no separate clarification record yet.
- No backfill of older mail.

## Definition of done

- Allowlisted, signed mail starts one work item, even when delivered twice.
- Unknown or unsigned mail waits, sends nothing and starts nothing.
- Each write mode behaves as described, and a dropped send stays for a
  person to resolve.
