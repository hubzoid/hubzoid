# Workflows from chat

The full plan, with mockups and every user-facing string, is
`HubzoidAgent/docs/hubzoid-workflows-from-chat-plan-2026-10-11.html`. This file
is the short version for the repository.

## Problem

People ask an agent for a report, then want it kept fresh and shared. Today:

- A code workflow takes no inputs, so "make the Bhava Spandana report for this
  month" can't be a workflow run.
- Chat can start a workflow only as an operator, as the workflow's account, and
  the asker can't read the result.
- A schedule is one per workflow, in code. A person can't keep their own copy
  fresh.
- `/portal/latest/<hub>/<key>` picks the newest copy from any account and
  redirects to a fixed copy, so a bookmark goes stale.
- Nobody outside the agent can read a report, even at the company's own domain.

IRS asked for live pages per program and per event, readable by anyone at the
company domain with the link. GitZoid needs reports with inputs, email to lists
and GitHub webhooks.

## Why now

IRS asked for live, shareable reports. GitZoid is being built against these
APIs. Both use the same pieces.

## What should happen

- A builder marks a workflow `as_tool=True` and gives it typed `inputs=`. Chat
  and MCP get a tool that runs it as the person asking ("Run for me").
- `schedulable=True` lets a person save a schedule with those inputs from chat.
  It runs as them and pauses with a named reason when access or inputs change.
  Nobody is emailed by default (amended 11 October, following Mastra, Agno and
  Sim): a hub turns notices on with `notify_by_email: true`.
- Every keyed report is a series: team (the workflow's own account) or personal
  (one person). The latest link serves the newest copy, shows when it updates
  next, and offers "Update now" to people who can run it.
- `audience="domain"` gives a series an unguessable `/r/<token>` link that
  anyone at an allowed company domain can open after signing in. They get a
  reader-only account with no agent access.
- The Console shows people's schedules, live reports with company links, and
  who started each run and whose access it used.
- One **Schedules & reports** page (`/portal/#/schedules`, amended 11 October)
  serves everyone: a person sees their own schedules and report links on every
  agent, with each schedule's latest runs, and pauses, resumes, runs or stops
  them; an admin also sees everyone's. Ownership is the verified account id and
  email. It is one tab of one portal for everyone (`/portal/`, amended 12
  October): people who aren't admins also get their Connections and the Agents
  they can use there, as **My workspace**, and Open WebUI's sidebar gets one
  Hubzoid link for every signed-in person ("Admin Console" or "My workspace").
  Admins find their own schedules and connections under **You** in the Console.
  The web app's account menu and the agent's replies link to it too.

## Scope and non-goals

Not in scope: chat writing workflow logic or prompt schedules, markdown tasks as
tools, DBOS schedules, moving schedules between people, posting results into a
chat thread, automatic page swaps, admins opening personal reports, public links
that follow the newest copy, agents inside workflows, a visual builder, report
text in emails to outsiders, custom chat cards.

Everything new is off until a hub opts in. Three changes reach every hub on
upgrade: the daylight saving fix in the dispatcher, the viewer's new look with
live latest links, and the new tables.

## Open questions

- Approve buttons in the web app for schedule confirmations (later).
- Token budgets stay with issue #6.

## Definition of done

- Each journey works on the test hub: builder CLI run, Run for me in the web app
  and over MCP, schedule, list, change and stop from chat, team run, live link
  with Update now, company link with a reader account, Console tables and
  Access rows, emails in the preview outbox.
- Same tool names and schemas on all three runtimes.
- A hub with none of the new options behaves exactly as before.
- Focused unit tests per item, one full `pytest -m ""`, and a Codex review.
