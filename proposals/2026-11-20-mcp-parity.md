# MCP at par with chat and workflows

Plan, 20 November 2026, on branch `feat/mcp-parity` (2.0.9 with #142, #144 and #146 merged).
Founder request: an assistant connected over MCP can do what chat and workflows can.

## Problem

The hosted MCP endpoint serves the hub's tools minus everything tied to a chat. A person in
Claude Code gets no files (`write_artifact`, `make_excel`, `make_pdf`, `make_word`,
`list_artifacts`), cannot hand the hub a file (`read_upload`, `read_upload_full`), cannot answer
a follow-up question or an approval in place (`ask_question` is off, and an approval only says
"the card under this message asks them"), cannot save shared knowledge (`remember` and the hub
change tools are never added), and never reaches a sub-agent that runs on its own model
(`delegates=[]`). Core skills such as `dashboard` are left out because their delivery tool is
missing. Isha's IRS hub (Odoo tools, ClickUp, skills from agents) uses these surfaces from chat,
and its people want the same from their own assistants.

## What should happen

1. **Files.** Over MCP each person has their own file space, `chats/mcp-<hash of account>/`,
   bound for the call exactly as a chat id is bound in chat. The file tools then work unchanged
   and return the same signed download link (`/artifacts/...?t=&e=`). Each file result also carries
   an MCP `resource_link` to `hubzoid-file://artifacts/<name>`, a resource template only the owner
   can read, so the assistant can read a file back. One new MCP-only tool, `upload_file(filename,
   content, encoding)`, saves a file the person gives their assistant (text, or base64 for binary)
   into the same space, within the deployment's upload size limit. `read_upload` and
   `read_upload_full` read it. MCP has no upload primitive, and the assistant already holds the
   person's local file, so the content travels in the call.
2. **Follow-up questions and approvals.** The endpoint keeps a session per connected assistant
   (MCP SDK session manager: 30 minute idle timeout, session cap, a session bound to the token that
   opened it). This is needed because MCP elicitation answers arrive on a second request. When the
   client declared the elicitation capability:
   * `ask_question` asks the person through elicitation and returns their answer.
   * An approval the requester may give themselves is asked through elicitation and recorded as
     their decision in the approvals store, then the call runs once, as after an inbox approval.
     An approval that needs someone else stays in the inbox.
   Without elicitation: `ask_question` tells the assistant to ask the person itself, and a waiting
   approval says where to approve (the inbox link) and to call again with the same arguments.
3. **remember and hub changes** are added to the MCP registry with the same gate as chat
   (`curator` grant or hub manager, in-app editing on). A manager's knowledge change applies, anyone
   else's is a proposal reviewed in Settings, Hub changes. Personal memory tools come with them.
4. **Model delegates behind a grant (B037).** A new capability, `mcp_delegates` ("Use sub-agents
   on this agent's model from an assistant"), off until granted. With it, each delegate is offered
   as its chat tool name (`handover_<name>`). A call runs the hub's own runtime once, as the caller
   on surface `mcp`, asking it to hand the task to that sub-agent (each runtime already wires its
   delegates: Agents SDK `as_tool`, Claude `AgentDefinition`, Codex). Usage is recorded under the
   caller with surface `mcp`, so budgets and the usage page see it.
5. **Workflows.** Already offered: `list_workflows`, `workflow_runs`, `run_workflow`, pause, resume,
   cancel, each "Run for me" tool and personal schedules. Verified end to end, wording fixed where
   it assumes a chat.
6. **Core skills** are advertised over MCP once the file tools are there.

`record_feedback` stays chat only: it rates a hub answer, and over MCP the answer is the
assistant's own.

## Why this shape (B038)

Sessions, elicitation, resource links and resource templates are MCP SDK and FastMCP features,
used as they are. Our own code is the per-person file space binding, the upload tool (MCP has no
upload), the elicitation bridge into the existing approvals store, and the delegate tool that calls
the hub's existing runtime.

## Scope and non-goals

No new runtime, no change to MCP tokens or `hubzoid/mcp_oauth.py`, no list-changed notifications
for connected apps (possible now that sessions exist, left for later), no MCP Apps changes, no
new upload route.

## Risks

* Sessions live in memory. A bridge restart ends them and the client opens a new one (the SDK
  answers 404 for an unknown session, as the spec requires). Clients that do not re-initialize
  must reconnect.
* Elicitation trusts the client to show the question to the person, as the MCP spec requires. Only
  approvals where the requester is an approver use it.
* A delegate call spends the hub's model on the caller's behalf. That is why it is a grant.

## Definition of done

Focused tests for each item. An end-to-end run on a copy of HubzoidTestHub with Claude Code
connected over MCP: a file with a working link, an upload read back, a follow-up question
answered, `remember` reviewed, a delegate with and without the grant, a workflow started and
followed, each next to the same task in chat.
