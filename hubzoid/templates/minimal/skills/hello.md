---
name: hello
description: Greets the user with a quick demonstration that skills work.
---

# Hello skill

You were loaded because the user said something like "hi", "greet me", or
asked you to demonstrate a skill.

1. Call the `hello` tool (defined in `tools_local/hello.py`) with the
   user's name if you know it, otherwise leave the argument blank.
2. Pass the tool's output through verbatim, then add one short sentence
   noting that this greeting went through one skill and one tool.

## When to use this skill

- The user's first turn is a bare greeting or "show me how skills work".
- Any other time, ignore this skill and answer normally.

Replace or delete this file when you write your real skills. Skills live
in `skills/`. The flat layout (`skills/<name>.md`) is fine for short
procedures; use a folder (`skills/<name>/SKILL.md`) when the skill needs
supporting files.
