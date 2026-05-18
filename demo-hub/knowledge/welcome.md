---
name: welcome
description: First-turn greeting content. The Guide reads this and paraphrases.
keywords: [welcome, greeting, intro, start]
---

# Welcome to Hubzoid

You are talking to the **Hubzoid Guide**, an agent that runs inside a
Hubzoid hub.

## What just happened

You ran `hubzoid run demo-hub` and the framework booted three things.

1. A FastAPI bridge on `:8000` exposing an OpenAI-compatible API.
2. Open WebUI on `:3080` pointed at the bridge as its model provider.
3. This agent. Its instructions live in `demo-hub/AGENTS.md`. Its
   knowledge lives in `demo-hub/knowledge/`. Its skills live in
   `demo-hub/skills/`. Open the folder. Read along.

## What this hub is

`demo-hub` is a working Hubzoid hub. Every concept you ask about, this
hub demonstrates. The Guide answers from its own structured knowledge,
not from open-web search.

## Try asking

- *What is Hubzoid?*
- *Show me the three agent types.*
- *What does an AGENTS.md look like?*
- *Build me an agent that drafts daily standup notes for my team.*
- *List the skills and knowledge in this hub.*
- *How do I add my own tool?*
- *Where do I learn more?*

## Next steps

- Replace `demo-hub` with a real hub. `hubzoid init my-real-agent`.
- Read the README at `https://github.com/hubzoid/hubzoid`.
- For enterprise deployments, see `https://hubzoid.com`.
