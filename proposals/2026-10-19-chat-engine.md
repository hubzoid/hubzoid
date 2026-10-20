# Chat engine: server-owned branches and queue, organisation and search, feedback and notices, sharing

## Problem
The native chat keeps its working state in the browser. One reply at a time
per conversation is enforced by a process-local flag, editing a message or
regenerating a reply rewrites the only line of work, a follow-up typed during a
reply is lost unless the person waits, and a reload during streaming relies on
polling one message. Two people cannot work in one conversation at all:
conversations are private to their owner, and a share is a frozen copy with no
audience control. There are no folders, tags, pins or full-text search beyond a
title filter, no way to move history in or out, no feedback on answers, and no
notice when a long reply finishes in another tab.

## Why now
Every later chat feature (files, retrieval, voice, model comparison, team
spaces) adds operations to a conversation. They need one place that decides
where a message goes, who may write, what runs and in what order, and how a
client catches up after a disconnect. Building them on the browser-owned
runtime would mean redoing each of them.

## What should happen
- **One operation service.** Every change goes through
  `POST /api/conversations/{id}/operations` with an `Idempotency-Key`:
  `send`, `respond`, `revise` (save only, or save and generate), `regenerate`,
  `continue`, `delete` (bound to a preview), `fork` and `synthesize`. A retry
  with the same key returns the first result. The CLI
  (`hubzoid chat operations submit`) and the web app call the same service.
- **Branches with one writer.** A branch is one person's line of work. New
  messages go after its head, by its writer; anything else is a clear
  conflict. Editing, regenerating or continuing opens a new branch for the
  person who asked, so nobody's work is overwritten. Different branches run at
  the same time, within configured limits. Which branch a person is looking at
  is their own choice and never moves anyone else.
- **A durable queue.** A message typed during a reply is queued on the server,
  in order, and sent when the branch is free. Stop parks the queue first, keeps
  the partial answer, and never claims a step already sent to a tool was undone.
  Removing a queued message parks the ones that depended on it.
- **Catch-up by sequence.** Each conversation has an event log with a strictly
  increasing sequence. A client that reconnects asks for what it missed; one
  that is too far behind is told to reload the snapshot.
- **assistant-ui external store.** The web app renders server state through
  assistant-ui's `useExternalStoreRuntime`; it never owns the graph. A contract
  test fails the build if the pinned adapter interface changes.
- **Organisation and search.** Personal folders with inherited defaults, pins,
  tags, bookmarks and archive, in the UI, CLI and API. Full-text search over
  conversations and messages using the database's own engine (SQLite FTS5 or
  PostgreSQL full text), filtered by access before ranking. A versioned
  export/import package moves conversations with all their branches.
- **Feedback, titles and notices.** Ratings with reasons and comments on an
  exact answer, visible to permitted managers. A title the person typed always
  wins over a generated one. An in-app inbox (plus optional desktop and sound)
  says when a reply completed, failed or was interrupted, and links to it.
- **Sharing.** A snapshot link is previewed, then published bound to that
  preview, for a named audience, the deployment, or (only when policy allows)
  the public. Revoking stops reads at once. Live collaboration adds editors and
  viewers to a conversation; editors write on their own branches. Copying a
  shared conversation gives an independent copy with no runs, queue or
  authority carried over.
- **A quieter interface.** Dark by default, compact text and spacing, and
  subtle borders in chat and Console, in the style of desktop coding tools.
  Light mode and administrator locks keep working.

## Out of scope
Budgets and spend limits, approval and question requests, model comparison,
voice, files and retrieval changes, team spaces, and PostgreSQL-only tuning.
External notification channels beyond the in-app inbox, desktop and sound are
modelled but not delivered here.

## Compatibility
`/api/chat` keeps its streaming contract and becomes a thin call into the
operation service. Existing conversations get one branch per leaf on first
read, owned by their owner. Deleting a message or conversation keeps a
tombstone with its content removed. Existing deployments keep working with no
new files; the new settings are optional namespaces of `chat/config.yaml`.
