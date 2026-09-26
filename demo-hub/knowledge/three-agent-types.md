---
name: three-agent-types
description: The canonical three agent types in Hubzoid. Tool, Q&A, Background.
keywords: [agent types, tool, qa, q&a, background, automated, scheduled]
---

# The three agent types

A useful way to think about the agents in a Hubzoid hub is three types.
Each maps to one of the three ways a team uses a hub: personal assistants
through MCP, chat, and workflows. One hub can serve all three from the
same context.

## 1. Tool / Action

**Runs on demand. You ask, it does.**

Generates a deck, drafts an email, builds a report, runs a query.
Designed to feel like a button you press, not a conversation. The output
is a file, a row in a system, or a posted message.

Lives in the personal assistants your team already uses, such as Claude
Code or Codex, connected to the hub through MCP. Also in scripts that call
the hub's API.

## 2. Q & A

**Conversational. Ask the hub anything.**

Answers questions in plain language using the hub's structured knowledge.
Talks the way the team talks. Stateless across questions unless the
operator wires persistent memory.

Lives in web chat, Slack, and other chat channels, each with its own
setup. The agent you are talking to right now is a Q&A agent.

## 3. Automated / Background

**Runs on a schedule. Answers before anyone asks.**

Watches numbers, flags anomalies, drafts weekly reviews, sends daily
digests. The work happens whether you log in or not. Background agents
typically chain multiple steps, fetch from external systems, and deliver
output by email or chat.

In Hubzoid, background work runs as workflows: Markdown tasks in
`schedule/` or Python workflows in `workflows/`, with recorded runs. A run
can publish a private report and email the person it runs as a link.

## How a team uses all three

A hub scoped to one team, product, or company can offer all three from
the same knowledge, skills, and tools.

- A **Q&A** agent in chat for the team.
- A **Tool** agent in a personal assistant for the person who wants
  on-demand outputs.
- A **Background** workflow that produces a weekly report.

This is why Hubzoid is not "an agent". It is a **hub** with as many
agents as the team needs.

## In this hub

`demo-hub` is a Q&A agent. The other two types are not running here. If
you want to see a Tool-flavor agent, hand off to the `builder` sub-agent
and ask it to draft one for you.
