---
name: explain-skills
description: Explains the Hubzoid skills system, using itself as the demonstration.
---

# How skills work

You just invoked a skill. The file you are reading right now is the
skill. Skills are markdown files at `<hub>/skills/<name>/SKILL.md`. They
load on demand via `load_skill(name)`.

## Why skills exist

System prompts cost tokens on every turn. Putting every possible playbook
into `AGENTS.md` makes every reply slower and the model dumber by drowning
the core instructions in detail.

A skill is a procedure the agent only needs **when it is needed**. The
main agent advertises skills as menu items (via `list_skills()`) and pulls
the body into context only when the user's request matches.

## When to use a skill versus a knowledge file

| Use a skill | Use a knowledge file |
|---|---|
| Procedure. "First do X, then Y." | Reference. "Here is what X means." |
| Multi-step playbook. | Single read. |
| Has acceptance criteria, output format, edge cases. | Definitions, facts, history. |
| Example: `inspect-this-hub`, `build-first-agent`. | Example: `what-is-hubzoid`, `three-agent-types`. |

When in doubt, write it as a knowledge file. Knowledge is cheaper to
write and easier to maintain. Promote to a skill only when the agent
needs to be walked through steps.

## Anatomy of a SKILL.md

```markdown
---
name: explain-skills
description: One-line summary. Used in load_skill's menu.
---

# Skill body

Walk the agent through the procedure. Use sections. Be concrete. Include
acceptance criteria at the end.
```

The body should read like an instruction manual the agent follows step
by step. Use sections (`##`) for the agent to navigate. Include examples
when the procedure has edge cases.

## How to add one

1. `mkdir -p <hub>/skills/<my-skill>/`
2. Create `<hub>/skills/<my-skill>/SKILL.md` with the frontmatter above.
3. Restart Hubzoid. The skill is now in the registry.
4. The agent will see it in `list_skills()` and can load it via
   `load_skill('<my-skill>')`.

## In this hub

Right now, `demo-hub/skills/` contains four skills. Try the others.

- `build-first-agent` walks you through creating a new hub.
- `inspect-this-hub` enumerates this hub's contents using the file tools.
- `find-the-docs` reaches out to hubzoid.com for current info.

## Acceptance criteria

After invoking this skill, the agent should be able to: define what a
skill is in one sentence, explain when to use a skill versus a knowledge
file, and walk a user through creating a new skill in their hub.
