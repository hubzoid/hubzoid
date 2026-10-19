# Slack on the shared channel contract

Status: proposed for 2.0. Builds on the channel contract
(2026-10-19-channels.md).

## Problem

Today's Slack adapter runs as a separate Bolt process in Socket Mode. Bolt
acknowledges an event before any handler runs, so a crash between the
acknowledgement and the reply loses the request. People are matched by the
email address Slack shows, which needs an extra permission and fails for
anyone whose Slack and Hubzoid addresses differ. Thread history is rebuilt
from Slack on every turn, and a reply that fails mid-post is either lost or
posted twice. Hosted deployments cannot use HTTP delivery at all.

## What should happen

- A Slack connection is a `channels/<id>.yaml` file with `provider: slack`,
  the workspace and app it answers, secret references for its tokens and the
  channel ids where it may answer.
- Both transports share one admission path. HTTP requests are verified with
  the signing secret on the exact bytes before parsing. Socket Mode uses
  `slack_sdk.socket_mode` so each envelope is acknowledged only after the
  event is saved.
- People are matched by Slack user id through an account link. Channels
  shared with another company stay refused.
- Each thread runs in the person's own conversation in the native chat
  engine. Replies go to the stored thread with a recovery marker in the
  message metadata, so an uncertain post can be found again instead of
  resent.
- `hubzoid channels slack manifest` generates the app manifest from the file,
  with fewer permissions than today (no `chat:write.public`, no email).
- `hubzoid channels slack import-legacy` writes a disabled channel file for an
  existing setup without copying any token value. Once a Slack channel is
  enabled, the old listener refuses to start, so one app never has two
  listeners.

## Scope and non-goals

- Approval and plan buttons in Slack wait for the shared approvals service.
- Slack Connect is not supported.

## Definition of done

- A mention, a direct message and a follow-up each get one reply in their
  thread, across retries and a restart, on both transports.
- An unlinked person, a channel not on the list and a shared channel never
  reach a model.
- An uncertain post is found by its marker or stays uncertain, never resent.
