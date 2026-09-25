# First-use and Console stabilization

Status: implementation authorized by the owner on 25 September 2026.
The owner approved the UX review and resolved its four discussion items.
This direct approval supersedes the usual proposal merge and plan gate for this work.

## Problem

A configured owner can administer a hub but fail its first chat. The chat model
list does not reflect entry access. Setup, Console status messages, tool progress,
and the workflow scaffold have misleading or broken first-use paths. The README
still presents older enterprise-first positioning.

## Scope

- Provision the configured initial owner once using the existing verified account.
  Keep account credentials in Open WebUI and permissions in Hubzoid. Never promote
  ordinary sign-ins or restore deliberately revoked grants on login.
- Match model visibility to usable hubs and make denials actionable.
- Repair owned bridge connection configuration and copyable CLI commands.
- Suppress the upstream changelog welcome without adding another introduction.
- Apply the current Studio tokens, local fonts, accessible controls, compact
  status patterns and consistent language throughout Console screens and states.
- Improve access/account guidance, workflow presentation, results, navigation,
  starter examples and usage accounting. Show readable schedules and raw cron.
- Preserve working chat and upstream administration controls. Keep the ownership
  boundary in operator documentation and code comments, not extra visitor copy.
- Rewrite the GitHub README and audit documentation against the product brief,
  implementation and website documentation routes. Keep public copy customer-free.

## Non-goals

Old workflow-history migration is deferred. Do not delete history as a shortcut.
Console workflow execution/recovery controls remain out of scope. No new auth
store, replacement chat UI, visual workflow builder, or product feature gating.
Production deployment and public publication are separate from readiness checks.

## Build order and verification

1. Owner provisioning, access visibility, connection config, CLI regressions.
2. Usage identity/background calls and tool progress, clean starter example.
3. Shared Console design and all screens, including loading/error/empty states.
4. README, operator docs, website docs and provenance.
5. Targeted tests, full Python suite, Console build/journey tests, doctor,
   fresh chat/workflow browser review and release-readiness record.

Track each approved UX item and actual evidence in the completion record.
A failing or unexercised rollout check must stay explicit.

The requested final upgrade/rollback rehearsal exposed another release blocker:
the CLI supervisor exited immediately after signalling children, so container
shutdown could interrupt SQLite cleanup and make restore refuse leftover WAL
files. The stabilization also waits for child shutdown, with a shared grace
period and forced termination fallback, for standalone and gateway operation.

## Owner follow-up: compact workspace (25 September)

The owner requested one landing page combining the overview and agent cards:
five totals (messages with conversation count, active users, tokens, workflow
runs, approximate cost), with period/refresh controls and a small update time.
Remove explanatory overview prose, the per-agent metrics table, duplicate Agents
navigation, the global Runs navigation entry and Console Manage accounts buttons.
Keep agent-level runs and existing deep links working. Close public sign-up by
default; administrators still add accounts through the existing chat admin panel.
Explicit operator overrides and existing persisted authentication settings remain
operator-controlled. No new account store or customer deployment migration.

Implementation touches the Console landing, cards, shell and routing; Open WebUI
signup defaults; matching operator docs and browser/env regression checks.
Verify five-card desktop/mobile layout, agent navigation/search, refresh errors,
period totals, account-default overrides, and the existing Console journeys.

## Owner security follow-up

The owner requested verification and repair of `.env`/database reads through
chat and MCP, including recursive `grep_data` without ripgrep. Synthetic canary
regressions reproduced the issue. A shared public-file boundary now excludes
credential files, database files/sidecars/signatures and private state before
reads. Both grep backends enumerate approved files, with symlink checks; name
loaders and current-chat upload paths apply the same boundary. Fix the adjacent
Jinja renderer bypass by using its advertised sandbox. Preserve current-chat
public uploads/artifacts, public knowledge, tools' neutral schemas and custom
operator code. Validate denied reads, recursive search with both backends, MCP
transport, normal content and the full model-free regression suite.

## Owner PostgreSQL follow-up

The owner reported that Docker's PostgreSQL profile breaks MCP authentication
and group access because the identity readers still open SQLite. Route API-key,
group, connected-server and OAuth reads through the existing shared accessor,
using SQLAlchemy for both databases and enforcing read-only connections. Keep
OAuth refresh's explicit write path. Honor `DATABASE_URL` and `DATABASE_SCHEMA`,
record shared gateway storage in the deployment manifest, retain the upload
directory hint, and deny conflicting or unavailable stores without SQLite
fallback. Exercise real PostgreSQL plus SQLite, MCP transport, revocations,
suspension/account replacement, OAuth refresh and gateway environment isolation.

## Owner SDK and Console follow-up

Recheck the reported scaffold command, workflow service identities, missing
curator capability, tab title, 1440px layout, amber helper contrast and compact
logo size. Existing fixes cover command order, Markdown identity validation,
Console title and the merged landing. Add the missing built-in curator catalog
entry, correct warning contrast and padded wordmark sizing, align current SDK
guides, and verify real API grants and synthetic browser journeys. Dark-palette
redesign and a new Codex runtime remain recommendations for discussion.

Owner follow-up: show token totals/input-output and approximate cost on each
agent card, using the already-authorized summary response and shared period.
Add ungranted, fictional restricted capabilities only to the private review hub
so the owner can exercise Console grants manually.
