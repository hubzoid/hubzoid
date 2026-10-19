# WhatsApp intake on the shared channel contract

Status: proposed for 2.0. Builds on the channel contract
(2026-10-19-channels.md) and email intake (2026-10-19-email-intake.md).

## Problem

Orders and invoices also arrive on a company's WhatsApp number. Today's
listener answers unknown senders with a "not registered" message, can lose a
message if it stops at the wrong moment, and replies without checking
WhatsApp's 24-hour rule.

## What should happen

- A channel file connects one number on the Meta Cloud API. Each signed
  message is saved before Meta is told it arrived, and goes to Intake with
  its media, where a person releases or rejects it.
- Replies are recorded writes under `customer_reply.send`, which is `off` or
  `approve` for WhatsApp. The person who released the message approves each
  exact reply. A reply outside the 24-hour window is held.
- Delivery receipts are recorded and shown with the reply.

## Scope and non-goals

- No automatic replies and no acknowledgement message.
- No message templates or consent records, so a business cannot start a
  conversation yet.
- Telegram stays on today's listener.

## Definition of done

- A redelivered message is one item, and nothing reaches the sender or a
  model before release.
- Draft mode is refused, approve sends once, and an uncertain send waits for
  a person.
- A held reply is not sent outside the window.
