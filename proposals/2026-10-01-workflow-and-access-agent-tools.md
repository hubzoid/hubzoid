# Workflow and access tools for agents

## Problem
A team's agent can answer questions about its Hub, but it cannot help with the
Hub's own operation. To check whether a workflow ran, start one now, pause a
noisy schedule or cancel a stuck run, someone has to log in to the server and
use `hubzoid schedule ...`. To see who has access to an agent, or why a person
has it, a manager has to open the Console. The access proposal tools that
shipped in 1.0.1 are switched on per hub with `HUBZOID_MANAGEMENT_TOOLS`, which
gives them to every manager at once.

## Why now
The product direction is one Hub used through workflows, chat and personal
assistants over MCP. The architecture says "an agent can start a workflow" and
that agent and workflow calls reuse the same authorization (layer 4, H13, H14).
The founder note on H16 asks to "allow authorized people to add others through
agent chat". OAuth for MCP brings assistants like Claude Code and Cursor to the
same Hub, where these controls are most useful.

## What should happen
- Two tool families, each off for everyone until a manager ticks a box in the
  Console access drawer, under **Hubzoid tools**, in its own section:
  - **Workflows**: See workflows and runs (`workflows_view`), and Run and
    control workflows (`workflows_manage`, sensitive). Tools: list workflows,
    recent runs and one run's steps, run now, pause, resume, cancel.
  - **Access control**: Manage access from chat (`access_tools`, sensitive,
    organization administrators grant it). Tools: my management scope, who has
    access, explain access, propose access change, propose new account.
- Every call is checked in code against the verified caller, never a tool
  argument. Workflow tool calls are recorded in the decision log, and every
  run control and access change in the access audit. Access changes still need
  confirmation in the Console.
- A run started from chat acts as the workflow's own account. The person who
  started it is recorded. Results stay visible only to the run's account.
- Workflow tools act only on the agent they run in. They are never offered on
  Slack or inside a scheduled run.
- `HUBZOID_WORKFLOW_TOOLS=false` and `HUBZOID_ACCESS_TOOLS=false` remove a
  family from an agent.
- One service (`workflows/control.py`) runs the controls for the CLI and the
  tools.

## Scope and non-goals
- Not creating or editing workflows from chat. A `schedule/*.md` file can run a
  shell command, so that needs draft and live revisions and approvals first.
- No Console run buttons in this change (they would reuse `control.py`).
- No durable approvals for workflow actions (H14).
- No cross-agent workflow control.

## Open questions
- Should run, pause and cancel need Console confirmation like access changes?
  This change says no: grant, chat confirmation and audit.
- `HUBZOID_MANAGEMENT_TOOLS=true` keeps its 1.0.x meaning for one release.
  Remove it in the release after.

## Definition of done
- An upgraded hub shows no new tools to anyone until someone grants them.
- A granted person lists, runs, pauses, resumes and cancels workflows from web
  chat and MCP. Activity shows who did it and from which surface.
- A manager with `access_tools` sees access and proposes changes. Without it,
  nothing is offered.
- Same tool names and schemas on all three runtimes. MCP lists only what the
  caller may use. Full test suite green and an end-to-end run on a test hub.
