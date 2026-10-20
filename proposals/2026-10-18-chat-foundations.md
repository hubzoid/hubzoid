# Chat foundations: one configuration contract, one theme, reviewed branding and admin diagnostics

## Problem
Hubzoid 2.0 has one chat, its own, and drops Open WebUI. Today the native chat
has no versioned settings: appearance is a per-device choice, Console and chat
keep separate colour tables, and branding is whatever files sit in a hub's
`branding/` folder, picked from the first hub when a gateway records nothing.
An unchecked `custom.css` can restyle or hide anything, including approval
buttons. A builder working through Claude Code or Codex cannot see which value
is in effect or why, cannot preview a brand before teammates see it, and cannot
roll one back. Operators have no read-only diagnostic report or support bundle
that is safe to share.

## Why now
Every other 2.0 chat feature (message engine, files, retrieval, voice, models,
sharing, team spaces, authoring) needs a place to declare its settings and a
theme to render in. These foundations have to land first, and the list of
what the new chat must preserve has to exist before features are called done.

## What should happen
- **Chat configuration.** An optional deployment file, `chat/config.yaml`
  (`version: 1`), declares chat settings. One schema backs the file, the CLI
  (`hubzoid chat config schema|get|validate|diff|apply`), the API
  (`/api/chat/config`) and the Console Settings page. Apply is all or nothing,
  takes the revision it expects and an idempotency key, and records an
  immutable revision. Every value shows its source: product default,
  deployment file, account or device. A fresh hub with no file works.
- **Personal preferences.** Each person may pick light, dark, OLED or system
  appearance, text size and density, within what the deployment allows. The
  choice follows their account across devices. A preference never changes
  who may approve anything or how anyone signs in.
- **One theme.** A validated token set (`branding/theme.json`) feeds both the
  chat and the Console, with contrast checked for every mode. Choosing an
  agent never recolours the application.
- **Branding with review.** `branding.yaml` plus files under `branding/`
  describe the deployment's name, logos, sign-in and welcome copy, links,
  optional terms and an announcement. A builder validates, creates a draft,
  previews it privately, reviews the exact digest and activates it. Rollback
  creates a new reviewed activation from an older revision. Unsafe SVG,
  oversize images and unrestricted CSS are rejected. Only a narrow decorative
  style subset is allowed, never on approval cards.
- **Admin settings and diagnostics.** An optional `admin.yaml` bounds
  diagnostic runs. Console Diagnostics runs read-only checks (storage, model
  sign-in readiness, scheduler, connectors, telemetry, configuration health),
  records a report, survives restarts as "interrupted" rather than rerunning,
  and produces an expiring, metadata-only support bundle. Monitoring commands
  gain `--json`.
- **Parity ledger.** A versioned, machine-checked list of what the new chat
  must preserve from Open WebUI and the selected LibreChat features, each item
  with one owning issue and an acceptance check. `hubzoid parity
  validate|coverage|candidate|review|freeze` seals a reviewed digest. Live
  deployment settings stay an explicit gap until an owner provides a redacted
  inventory.

English is the only shipped language. Strings stay in one catalog so another
language can be added later as a file.

## Scope and non-goals
- Not sign-in methods, groups or the permission registry. Until those land,
  the new permission names map onto today's grants (deployment and hub
  `manage_access`, or the local owner when sign-in is off).
- Not the message engine, files, retrieval or any later chat feature. They
  register their own namespaces in `chat/config.yaml` later.
- Not backup, restore or upgrade screens.
- No new language catalogs, right-to-left layout or language menu.
- `custom.css` is no longer served. Existing decorative needs move to the
  restricted decorative slots, and the legacy import reports anything dropped.

## Open questions
- Whether hosted MCP should expose the read-only admin health tools by
  default to deployment admins, or only after an explicit grant. The proposal
  is an explicit grant.

## Definition of done
On a running test hub: a builder applies `chat/config.yaml` from the CLI and
sees the same revision and sources in the Console. Two applies against the
same revision give one winner and a clear conflict. A teammate's dark or OLED
choice follows them to another browser. A branding draft is previewed, its
exact digest reviewed and activated, then rolled back, with sign-in, chat,
share and Console all showing one brand. An unsafe SVG and an unreadable
palette fail with a named reason. A diagnostic report runs, is interrupted by
a restart without rerunning, and yields a support bundle without secrets or
chat content. The parity ledger validates, reports its gaps and freezes only
against a reviewed digest.
