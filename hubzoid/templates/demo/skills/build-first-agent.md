---
name: build-first-agent
description: Walks the user through scaffolding their first real Hubzoid hub. Hands off to the builder sub-agent for the drafting work.
---

# Build your first real agent

Follow this playbook when the user says any of:

- "How do I build my first agent?"
- "Walk me through making an agent for X."
- "I want a hub that does Y."

## Step 1. Get the use case clear

Ask the user one focused question before drafting anything.

> "What is the one job this agent should do well? Examples: summarize my
> Notion notes each morning, answer team questions about our HR policies,
> draft sales follow-up emails from a CRM dump."

If they already gave you a clear answer, skip the question.

## Step 2. Hand off to the builder sub-agent

The `builder` sub-agent is purpose-built for this. Hand off with the
use case as the prompt. The builder will draft:

- A frontmatter block (name, description, model).
- A system prompt body shaped for the use case.
- One starter skill if the use case warrants it.
- A list of tools the agent needs.

The builder's output is a draft. You do not write the files. The user
does.

## Step 3. Show the user how to materialize the draft

After the builder returns its draft, tell the user the exact commands to
run.

```bash
hubzoid init <agent-name>
# replace the scaffolded AGENTS.md with the draft above
hubzoid run <agent-name>
```

If they are inside the demo-hub directory and want to put the new agent
alongside it, the first command should be run from the parent directory:

```bash
cd ..
hubzoid init <agent-name>
```

This is where the agents-repo wrapper kicks in. The first `hubzoid init`
in a fresh directory writes a `requirements.txt`, `.gitignore`, and
`README.md` at the parent level. Subsequent `hubzoid init` calls in the
same directory only add new hubs.

## Step 4. Point at the next layer

If the user wants more than a single agent (multiple hubs, a shared
team, scheduled workflows), tell them about the wider picture:

- The README on GitHub explains multi-hub agents repos.
- For enterprise deployments, `hubzoid.com` is the consulting practice
  that does the build for you in six weeks.

## Acceptance criteria

A successful run of this skill ends with the user holding a draft
`AGENTS.md` they can paste into a new hub, plus the three commands to
materialize it.
