# Approvals and questions: the agent asks a person before it acts

## Problem
An agent can only run a tool or refuse it. Teams want some actions to wait
for a person ("reserve 4 units?"), want the agent to ask when a detail is
missing, and want long jobs to share a plan that someone approves before work
starts. Today there is no request a person can answer later, no record of who
approved what, and no way for a chat to continue once they do.

## Why now
The ready-made hubs (software, orders, customer requests) all need a person
to approve exact actions and plans. Hosted teams need the same answer to work
from chat, the Console and messaging channels.

## What should happen
- **Off unless asked for.** Every tool runs without asking by default. An
  optional hub file, `hitl.yaml`, sets `ask: never | once | always | auto`
  per tool or per MCP connector. A connector can use `ask: writes`, which
  asks for any tool its server does not mark read-only.
- **One card.** A tool that must wait shows an approval card in the chat with
  the exact action: Approve, Approve for this chat, Reject with a note, and
  Always allow for me where the builder allows it. The agent says it is
  waiting and ends its turn. Nothing runs while it waits.
- **The chat continues.** After a decision the person's choice appears as
  their reply and the agent carries on in the same conversation. An approval
  admits that exact call once, so the action runs once, from the agent's own
  call. A rejection tells the agent not to. Writing something else instead
  cancels the wait.
- **Questions.** The agent can ask one question with two or three options or
  a typed answer, following the shape of a well-tested open-source agent
  framework's question tool. Builders can turn questions off.
- **Who decides.** The person who asked, or people named in `hitl.yaml`. A
  person decides in their own browser session; API keys, MCP tokens and
  models never can. An unattended run is refused instead of waiting.
- **Everywhere it is shown.** The chat card, a Console list of what waits for
  you, a review page and links, and the same requests on messaging channels.
- **Workflows and plans.** Workflows can stop at `hub.approve` and `hub.ask`
  steps that wait durably for a named person, and `hub.review_plan` asks for
  approval of one exact plan document.
- Files, CLI (`hubzoid hitl`, `hubzoid requests`) and API
  (`/api/requests`, `/api/hitl/config`) do everything the UI does.

## Out of scope
Approver groups (they arrive with groups), typed multi-field forms, and
anything that lets a model approve on a person's behalf.

## Compatibility
Additive. A hub without `hitl.yaml` behaves as before. One new set of
operational tables.
