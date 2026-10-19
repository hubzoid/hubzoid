# Hub changes from chat, hub memory and personal memory

## Problem
People correct the agent in chat ("the reorder level is 15 now", "always say
which warehouse"), but the fix lives only in that chat. The old `remember`
wrote straight into the hub with no review, and nothing kept what a person
asked the agent to remember about them.

## Why now
Teams share one agent. A change to its instructions or knowledge should be
reviewed once and recorded, and a person's own preferences should follow them
without touching the shared hub.

## What should happen
- An agent can read and propose exact edits to `AGENTS.md` and Markdown under
  `knowledge/` and `skills/` (`read_hub_file`,
  `propose_hub_change`, `hub_change_status`). Each edit replaces text that
  appears once, appends, or creates a new file.
- A proposal records each file's bytes when proposed. Applying writes the
  exact reviewed text only if the files are unchanged; otherwise it is a
  conflict and nothing is written.
- Proposing needs Save shared knowledge (`curator`) or managing the hub.
  Applying or rejecting needs managing the hub, checked when deciding, from a
  person's own session (never an API key or a script). A change an agent
  proposes never applies itself, even for a manager, so a prompt-injected
  call can't change shared knowledge: a manager applies it with one click on
  the chat card. A manager's knowledge change from their own terminal applies
  at once.
- Applying holds a hub write lock shared with scheduled-task commits and, in
  a Git repository, commits only those files with `Hubzoid-Change`,
  `Hubzoid-Proposed-By` and `Hubzoid-Approved-By` trailers. A crash part way is
  finished on the next start by searching for the trailer: one commit, never
  two. With `HUBZOID_HUB_CHANGES_PUSH=1` the commit is pushed as it is; a
  refused push needs a person, and scheduled pushes never rebase a reviewed
  change.
- Undo is a new proposal with its own review, refused when the files changed
  since.
- Access, approval and run settings, workflow code, tools, `.env` and
  anything that looks like a secret are never proposable.
- `remember` is hub memory: it adds to or corrects the topic's file in
  `knowledge/_learned/` through the same path, so it is applied from the card.
- Personal memory: one short note per person per hub in the operational
  database. `save_memory` and `remove_memory` change it from chat; Settings,
  Memory shows, edits and deletes it. Each chat starts with it, and the hub's
  own instructions win when they disagree.
- Where: chat card with the diff, Settings, Hub changes (waiting, applied,
  needs attention), `hubzoid hub-changes list|show|propose|decide|undo`, and
  `/api/hub-changes`, `/api/memory`.

## Out of scope
Publishing through a GitHub App, remote checkouts, moving GitZoid's rule
drafts onto this path, and memory written without a person asking.

## Compatibility
Additive: two operational tables. `remember` keeps its name and gate; it now
edits through a proposal instead of replacing the whole file, and hub
managers may use it without the separate grant.
