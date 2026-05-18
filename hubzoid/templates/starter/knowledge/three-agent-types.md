---
name: three-agent-types
description: The canonical three agent types in Hubzoid. Tool, Q&A, Background.
keywords: [agent types, tool, qa, q&a, background, automated, scheduled]
---

# The three agent types

Every agent that ships in a Hubzoid hub is one of three types. The
taxonomy is load-bearing. Customer-facing language, customer-facing folder
structure, and product design all use it.

## 1. Tool / Action

**Runs on demand. You ask, it does.**

Generates a deck, drafts an email, builds a report, runs a query.
Designed to feel like a button you press, not a conversation. The output
is a file, a row in a system, or a posted message.

Lives in Claude Code, in-app buttons, IDE extensions, scripts.

## 2. Q & A

**Conversational. Ask the hub anything.**

Answers questions in plain language using the hub's structured knowledge.
Talks the way the team talks. Stateless across questions unless the
operator wires persistent memory.

Lives in Telegram, Slack, WhatsApp, web chat. The agent you are talking
to right now is a Q&A agent.

## 3. Automated / Background

**Runs on a schedule. Answers before anyone asks.**

Watches numbers, flags anomalies, drafts weekly reviews, sends daily
digests. The work happens whether you log in or not. Background agents
typically chain multiple steps, fetch from external systems, and deliver
output by email or chat.

Lives in email, Telegram digest, dashboards. In Hubzoid, background
agents run on WaveAssist Cloud (the sister infrastructure).

## How a real deployment uses all three

A single role-scope (`/marketing`, `/finance`, `/ops`) typically gets one
of each.

- A **Q&A** agent on Telegram or Slack for the team.
- A **Tool** agent in Claude Code for the role owner who wants on-demand
  outputs.
- A **Background** agent in email that sends a weekly digest.

This is why Hubzoid is not "an agent". It is a **hub** with as many
agents as the team needs.

## In this hub

`demo-hub` is a Q&A agent. The other two types are not running here. If
you want to see a Tool-flavor agent, hand off to the `builder` sub-agent
and ask it to draft one for you.
