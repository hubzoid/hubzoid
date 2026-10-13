# Hubzoid · Product direction

Direction revision: HZ-DIRECTION-2026-10-07-v5. Vision approved: 2026-09-22. Adoption and services direction updated: 2026-10-04. Value message, first-use direction and customer-hosting requirement updated: 2026-10-07.
This is the public-safe development brief derived from the founder-approved product direction. It describes intent and principles, not a release feature list. SDK documentation and tested release behavior establish current support.

## Vision and mission

Every team can put its knowledge to work through internal AI agents it owns and controls.

Help AI-capable people turn useful personal agents into reliable capabilities their whole team can use.

Core tagline: **One brain for your internal agents.**

Additional value message: **Your knowledge. Your way of working. Useful across your assistants and your team.**

Provide context once. Reuse it across workflows, chat, and the AI tools your team already uses. The additional message explains the personal and team benefit. It complements the core tagline.

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

Their adoption progression is: "It does useful work my way. I want to use it again. How do I give it to my team?"

Support the builder, the teammate using the result, and the administrator responsible for access and operation. A paid AI subscription, job title, company size, or industry is not an eligibility gate.

## Make it theirs

Help people build agents for their own work and company around knowledge and ways of working they maintain in a shared Hub. Assume the primary builder already uses an AI coding assistant such as Claude Code or Codex. Make that the main authoring path, alongside human-readable instructions. Authoring, MCP access and runtime selection are distinct operations.

Start with one real task, a small set of user-selected materials, their rules and an example of a good result. Help them organize these into explicit Hub knowledge and skills, inspect what was saved and run the task. Setup conversation alone does not become shared knowledge or automatic memory.

A runnable sample provides a starting point. The meaningful first-use milestone is a useful result based on the builder's own material and rules. They can correct a rule, explicitly save it and verify that the next result reflects it. This is an ordinary edit to Hub instructions, knowledge or skills and does not depend on a new memory system. Then they can repeat the work, add an agent using the shared foundation, connect a supported assistant or give a teammate appropriate access.

These are experience goals to implement and validate against the selected release. They do not establish automatic agent conversion, universal assistant support or a new memory feature.

## From your laptop to your team

The product should support the move from a useful prototype to dependable team operation, including appropriate access, execution, recovery, visibility, maintenance, and ownership. This is the meaning of "from POC to production" here. It does not promise unchanged deployment of every arbitrary prototype.

## Easy customer hosting

The framework must support an easy path for customers to host their own Hub and agents. Railway and Maritime are requested provider examples. This is part of the move from personal usefulness to team operation, with documented deployment configuration or templates and human and agent-readable instructions. It does not depend on buying implementation services.

Before promoting a provider path, validate the selected release, model authentication, secrets, persistent data, public URLs and callback configuration, access controls, costs, troubleshooting, backup/restore and upgrades. A useful task and permitted teammate access must work, with persistence through restart and verified workflow/scheduler behaviour, including any sleep/wake limits.

This is an approved framework requirement. Provider-specific implementation and support remain to be validated. Optional Hubzoid-managed hosting and a public demo remain separate decisions. This requirement does not provision an instance or establish that a personal model subscription supports unattended or shared-team hosting.

## Principles

- Internal-first: build for the organization's own people. Open-source users may apply the software elsewhere without changing the product focus.
- Fully open-source Hubzoid product code, including team controls. Dependencies and optional services retain their own terms.
- Minimize total ownership cost. Prefer existing solutions and own the gaps that demonstrably matter.
- Preserve practical interoperability and portability through supported integrations and understandable context.
- Make customer-controlled hosting straightforward, including validated provider paths. Optional Hubzoid-managed hosting can coexist with it. Offline operation depends on the selected stack.
- Make reliability, authorization, accountability, and administration part of the product experience.
- Give builders sensible defaults and an approachable first result.
- Distinguish intended behavior from implemented, tested, released, and observed behavior.

## Required roadmap outcomes

Required areas are stability and trust, building useful agents from the user's own knowledge and working rules, easy customer hosting, Build and Use modes, human-in-the-loop workflows and actions, RAG and knowledge retrieval, controlled memory, and multi-agent communication. These are approved roadmap outcomes, not a claim that every capability is shipped or committed to one release.

Start with a coding-assistant builder skill, a proposed Git-backed draft/review/activation workflow, one hosted deployment path and bounded examples for approvals, retrieval, memory and delegation. Prefer existing components. Preserve runtime neutrality, appropriate access and compatibility with existing Hubs. Keep personal-owner review simple and Git mechanics out of the first-use interaction. Any selected Build mechanism must preserve existing Hubs without new required files or Git layout. Detailed storage, protocols, native Build interface and release allocation remain to be scoped.

## Memory

Controlled memory is required roadmap scope. Assess shared central, hub-specific, agent-specific, and per-user memory/context. Boundaries, inheritance, visibility, and update authority remain open design questions. Conversation history, operational state, and learned knowledge are different concerns.

The direction is a Hub that improves through experience with control over what is remembered and how shared knowledge changes. Autonomous self-evolution is not an established feature claim.

## Product focus and adoption

Hubzoid is the single product focus. Its workflow experience is part of that product. GitZoid is a proposed sample engineering application, not a separate product growth programme.

Grow independent adoption and sustained use among AI-capable builders and experimenters, including non-engineers using coding or desktop assistants. The adoption pipe covers discovery, interest, understanding, confidence to try, ease of trying, a first useful result, and continued use or team deployment. Useful usage is the outcome. Stars, issues, contributions and recommendations are welcome but optional. Documentation, examples, onboarding clarity and support are part of growth.

Maintain a separate Build with us services path for business and technology leaders, mainly small and medium businesses and sometimes enterprises. Explain the internal agents we can build, business outcomes to measure, the delivery process, training, handover, deployment control and support. Use clear demo or quote actions. Build with us offers help creating the customer's own company agents around the same shared foundation. Self-directed product adoption remains the main product journey. No pricing, default hosting provider, commercial feature gate, outcome guarantee or delivery promise is selected by this brief.

After the 1.1.1 release, prioritize stability, testing and friction in first and repeat use. Older architecture plans are reference inventories rather than automatic implementation queues. Porting instructions and named assistant integrations require their own compatibility evidence. Connecting through MCP, adapting an agent and hosting a runtime are different operations.

## Chat interface support · 2026-10-13

Native chat (built with assistant-ui) and Open WebUI are permanently supported
interfaces with equal support and release-testing requirements. Native chat is
the default. Open WebUI is an optional install, not a temporary compatibility
mode, and no removal of either interface is planned. This decision supplements
HZ-DIRECTION-2026-10-07-v5 and supersedes earlier removal wording.

Validate shared capabilities and each interface's supported user journeys for
both modes before a release. Equal testing is a release requirement; it must
not be inferred from installing both interfaces in CI. Record actual results,
skips and gaps against the [release validation policy](RELEASING.md#chat-interface-validation).

Today, `pip install hubzoid` includes native chat and `hubzoid[openwebui]` adds
Open WebUI. Symmetric `hubzoid[assistantui]` and `hubzoid[openwebui]` extras may
be considered later to clarify interface selection. The `assistantui` extra
does not currently exist; no packaging change is approved by this decision.

## Open decisions

Implementation designs for the required roadmap, additional onboarding variants and optional Hubzoid-managed hosting require their own assessments. Easy customer hosting is required, with provider-specific implementation and validation still to be scoped. Native chat remains the default, with Open WebUI equally and permanently supported. Interfaces beyond these two are not a standing expansion commitment. This brief authorizes no platform migration. Public statements must reflect the selected release and verified evidence.
