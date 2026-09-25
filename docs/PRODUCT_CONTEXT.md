# Hubzoid · Product direction

Direction revision: HZ-VISION-2026-09-22-v1. Approved: 2026-09-22.
This is the public-safe development brief derived from the founder-approved product direction. It describes intent and principles, not a release feature list. SDK documentation and tested release behavior establish current support.

## Vision and mission

Every team can put its knowledge to work through internal AI agents it owns and controls.

Help AI-capable people turn useful personal agents into reliable capabilities their whole team can use.

**One brain for your internal agents.** Provide context once. Reuse it across workflows, chat, and the AI tools your team already uses.

## Why the Hub

Context takes effort to build. That investment should carry across agents, tools, and workflows, and improve as the team learns.

A Hub brings together context, knowledge, skills, agent definitions, connectors, external MCP capabilities, scripts, and the resources needed for recurring work. Markdown is an understandable and versionable foundation. Raw data can remain in source systems, and credentials and execution state have their own requirements.

"Provide context once" means establish a shared foundation, maintain it centrally, and reuse the relevant parts. It does not mean context never changes or everyone receives identical access. Hubs may be scoped to a product, team, or company.

## Three experiences

- Workflows: repeatable work on demand, on a schedule, or in response to events.
- Chat: teammates ask questions and request actions through useful working channels.
- Existing personal assistants through MCP: reuse the Hub's context, skills, and authorized capabilities in a supported assistant.

The experiences can overlap. They need not share identical conversation histories or tool capabilities. Verify integrations individually.

## Who it serves

People who already use AI effectively, understand a real business workflow, and want to make their agents useful beyond their own laptop. They may work in operations, finance, analysis, engineering, consulting, or leadership.

Their adoption moment is: "This works for me. How do I give it to my team?"

Support the builder, the teammate using the result, and the administrator responsible for access and operation. A paid AI subscription, job title, company size, or industry is not an eligibility gate.

## From your laptop to your team

The product should support the move from a useful prototype to dependable team operation, including appropriate access, execution, recovery, visibility, maintenance, and ownership. This is the meaning of "from POC to production" here. It does not promise unchanged deployment of every arbitrary prototype.

## Principles

- Internal-first: build for the organization's own people. Open-source users may apply the software elsewhere without changing the product focus.
- Fully open-source Hubzoid product code, including team controls. Dependencies and optional services retain their own terms.
- Minimize total ownership cost. Prefer existing solutions and own the gaps that demonstrably matter.
- Preserve practical interoperability and portability through supported integrations and understandable context.
- Preserve a usable self-hosted route. Optional managed hosting can coexist with it. Offline operation depends on the selected stack.
- Make reliability, authorization, accountability, and administration part of the product experience.
- Give builders sensible defaults and an approachable first result.
- Distinguish intended behavior from implemented, tested, released, and observed behavior.

## Memory

Explore shared central, hub-specific, agent-specific, and per-user memory/context. Boundaries, inheritance, visibility, and update authority remain open design questions. Conversation history, operational state, and learned knowledge are different concerns.

The direction is a Hub that improves through experience with control over what is remembered and how shared knowledge changes. Autonomous self-evolution is not an established feature claim.

## Product focus and adoption

Hubzoid is the single product focus. Its workflow experience is part of that product. GitZoid is a proposed sample engineering application, not a separate product growth programme.

Grow independent adoption and sustained use. Help people discover, understand, try, get a useful result, keep using, and share or contribute. Documentation, examples, onboarding clarity, and useful participation are part of growth.

An enterprise request path may offer implementation or deployment assistance. It is secondary to product adoption. No new pricing, hosting provider, commercial feature gate, deadline, or delivery promise is selected by this brief.

## Open decisions

Features, architecture, frontend choices, onboarding implementation, memory design, managed hosting, and the detailed launch plan require their own assessments. This brief authorizes no platform migration. Public statements must reflect the selected release and verified evidence.
