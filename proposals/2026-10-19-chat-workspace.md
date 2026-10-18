# Chat workspace: files and rich answers, sources and search, voice, models, team spaces, authoring, devices, migration readiness

## Problem
With one chat engine in place, the native chat still lacks what teams use every
day in the chat they are replacing: uploaded files with lasting identity, rich
answers (maths, diagrams, code, tables) and a safe artifact viewer; answers
grounded in their own documents and, when allowed, the web; dictation and
read-aloud; a choice of model and side-by-side comparison; shared channels,
notes and calendar for a team; a way to author agents, skills and prompts from
the chat itself; and dependable behaviour on phones, slow networks and
keyboards. Deployments still on Open WebUI need to know what moving will take
before anything moves.

## Why now
These are the last capabilities between the native chat and the chat it
replaces. Each builds on the operation engine, the configuration contract and
the theme that are now in place.

## What should happen
- **Files.** Uploads get an immutable id and checksum, a storage adapter
  (local disk, or one S3-compatible store), reference-aware reuse and deletion,
  and no default quota or automatic deletion. Answers render maths, diagrams,
  code with diffs, tables and media. Artifacts open in a sandboxed viewer with
  preview, source, versions, download and full screen, with no network access
  and no third-party bundler.
- **Sources.** People and builders keep collections of their own material.
  Answers retrieve from them with built-in text extraction and local lexical
  search, and cite the exact source passage. Web search uses the runtime's own
  search tool where it exists, for people granted it. Every public fetch passes
  public-network checks.
- **Voice.** The browser's own dictation fills the draft (never sends) and its
  own voice reads an answer aloud. No speech provider or key. Members need a
  grant.
- **Models.** People pick from the models their runtime offers, keep personal
  and shared presets, and (with a grant) compare the same turn on two models on
  separate branches, then keep one, keep both or merge.
- **Team spaces.** Channels with threads, mentions of people and agents,
  reactions and pins; notes with revisions; a calendar with events and
  reminders that run through the scheduler.
- **Authoring.** Agents, skills and prompts are edited as the hub's own files,
  validated, previewed as a diff, and published as a reviewed revision. Prompt
  variables insert a preview and never send.
- **Devices.** Install metadata and a share target, an offline shell that never
  caches API responses, a connection banner, send-key and touch-Enter choices,
  configurable keyboard shortcuts, a contrast option and touch-size targets.
- **Migration readiness.** An inventory and readiness report for a deployment
  still on Open WebUI: what maps, what needs a decision, and what blocks a move.
  Nothing is migrated by this work.

## Out of scope
Realtime voice calls, image generation, code execution, vector databases,
additional web-search providers, app marketplaces, encrypted temporary
sessions, and the migration run itself.

## Compatibility
All new settings are optional namespaces of `chat/config.yaml`; existing hubs
keep working with no new files. Existing uploads keep their ids and downloads.
Member features stay off until granted.
