# GitHub and Linear on the shared channel contract

Status: proposed for 2.0. Builds on the channel contract
(2026-10-19-channels.md).

## Problem

Engineering work starts in GitHub issues and pull requests and in Linear.
Today the only GitHub path is the GitZoid sample, with hand-built App tokens
that are not narrowed to a repository, and there is no Linear path at all.
A hub cannot be delegated a Linear issue, and a builder hosting their own hub
has no way to get a GitHub App that their own organization owns.

## What should happen

- **GitHub.** A connection is either the customer's own GitHub App, created
  through GitHub's manifest flow so the private key goes straight to the
  secret store, or a personal token for a hub on a laptop. Every call uses a
  token narrowed to one repository and the permissions it needs. Mentions by
  repository members, the configured label added by someone with triage
  access or more, new pull requests and failed checks start work. Replies go
  to the same issue, pull request or review thread, with a hidden marker so an
  uncertain post can be found again. Agents and workflows get the usual GitHub
  reads and writes as tools: writes are recorded, unattended work writes only
  where it came from, a new pull request is always a draft, and updating a
  pull request waits for a person's approval.
- **Linear.** The customer creates an OAuth app in their workspace and an
  admin approves it as an app. Delegating or mentioning starts work in that
  agent session, with an acknowledgement within seconds, follow-ups and Stop.
  Deliveries are checked against the webhook secret and a 60-second timestamp.
  The token renews itself, one worker at a time.

## Scope and non-goals

- Which branches a hub owns and how often it may repair CI is the hub's own
  rule, not the channel's.
- Updating a pull request waits for the shared approvals service.
- The GitZoid sample keeps its own client.
- Linear's native plan checklist (a preview) is not used, and the Linear token
  is not shared with other tools.

## Definition of done

- A customer creates and installs their own App, and nothing activates for
  the wrong owner or installation.
- Each trigger starts one piece of work and its replies stay in place, across
  repeats and restarts.
- A Linear delegation gets its acknowledgement and answer in its own session,
  and a grant for the wrong workspace is refused.
