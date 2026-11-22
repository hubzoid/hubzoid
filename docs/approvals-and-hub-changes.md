# Approvals, questions, workflow access, hub changes and memory

This page covers the ways a person stays in the loop: the agent asking before
it uses a tool, workflows that wait for a person, who may run which workflow,
repeated requests, changing the hub from chat, and memory. Each section gives
the chat experience first, then the file, CLI and API forms.

```yaml
# hitl.yaml at the hub root: ask before reserving stock
version: 1
tools:
  reserve_stock: {ask: always}
```

## Approvals and questions

By default the agent asks nobody before using a tool, and it may ask the
person a question. A hub chooses the tools that should ask in an optional
`hitl.yaml` at the hub root:

```yaml
version: 1
questions: true            # the agent may ask the person a question
approvers: requester       # or a list of emails or account ids
tools:
  reserve_stock: {ask: always}
  send_report: {ask: once, allow_always: true}
connectors:
  github: {ask: writes}    # MCP tools the server does not mark read-only
  crm: {ask: always}
agents:
  intake: {questions: false}
```

| Setting | Default | What it does |
|---|---|---|
| `questions` | `true` | The agent may ask the person a question. `agents.<name>.questions` overrides it per agent |
| `approvers` | `requester` | Who decides: the person who asked, or a list of emails or account ids |
| `ask: never` | yes | Never asks |
| `ask: once` | | Asks the first time in a chat |
| `ask: always` | | Asks every time |
| `ask: auto` | | A quick model check runs routine calls and asks a person about anything risky |
| `ask: writes` | | Connectors only: asks for tools the server does not mark read-only |
| `allow_always` | `false` | Adds "Always allow for me" to that tool's card |

A tool's own rule wins over its connector's.

**In chat.** When a tool must ask, the agent stops and a card appears under its
message. The card offers Approve, Approve for this chat, Always allow (when the
hub allows it), and Reject with a note. "Exact action" shows the arguments
exactly as they will run. After a decision, the same answer continues. Writing
a new message instead of deciding cancels the wait. Questions work the same
way: pick an option or type an answer.

**Who decides.** Only the person who asked, or a named approver. An admin can't
decide for them, and nor can the agent. To approve, answer or ask for changes
they must be able to use this agent (**Use this agent**): someone whose access
was removed, or a named approver never given it, can only reject. A person may also decide through a
personal API key they made with the `approve` operation ([API](api.md#decide-approvals-and-questions)).
No other key, machine token or MCP token can decide.

**Where requests show.** The sidebar's Inbox lists every open request across
your agents, with a count. Slack shows approvals as buttons in a private
message. WhatsApp uses numbered replies. Email has a link to the page. A
`request.created` webhook can tell your own system that a request waits
([webhooks](api.md#receive-webhooks)).

### CLI and API

```bash
hubzoid hitl validate ./my-hub --file hitl.yaml
hubzoid hitl export ./my-hub            # the active rules and their revision
hubzoid hitl apply ./my-hub --file hitl.yaml --expected-revision <revision from export>
hubzoid requests list ./my-hub --user ana@example.org
hubzoid requests decide ./my-hub <id>   # in your own terminal, asks you to retype a code
```

| Route | What |
|---|---|
| `GET /api/requests` | What waits for you |
| `GET /api/requests/{id}` | One request, with what you may do |
| `POST /api/requests/{id}/decisions` | Decide. A person's session or an `approve` key only |
| `POST /api/requests/{id}/cancel` | The requester withdraws it |
| `GET /api/requests/events?after=N` | Request events after a sequence number |

## Workflows that wait for a person

A Python workflow can stop and wait. The run holds no process while it waits,
and it continues after a restart:

```python
from hubzoid import hub, workflow

@workflow(as_tool=True)
def launch_plan_review():
    region = hub.ask("Which region?", options=["India", "US", "Europe"])
    ok = hub.approve(f"Launch in {region}", details={"region": region})
    if not ok["approved"]:
        return {"launched": False, "note": ok["note"]}
    plan = hub.data_dir / "plan.md"
    plan.write_text(f"# Launch plan for {region}\n...")
    review = hub.review_plan(plan, title="Launch plan")
    return {"launched": review["approved"], "region": region}
```

| Call | Returns |
|---|---|
| `hub.ask(question, options=None, approvers=None)` | The answer |
| `hub.approve(action, details=None, approvers=None)` | `{"approved", "note", "by"}` |
| `hub.review_plan(path, title=None, approvers=None)` | The person's approval or requested changes, with a note |

`review_plan` publishes the exact plan. A revised plan is a new version that
needs its own approval. Named approvers can open the plan.

When you start a workflow from chat ("Run Launch plan review for me"), its
questions and approvals appear in that chat as cards, and also in the Inbox.
When the run ends after the chat stopped waiting, its result comes back to the
same chat as a message. A run nobody started from chat, such as a scheduled
one, emails the person a link to the Inbox.

## Who may run, schedule and change each workflow

Settings, Workflow access gives separate rights per workflow:

| Right | Notes |
|---|---|
| run | |
| schedule | Needs run too |
| edit, create | |
| review changes | |
| see definition, see runs, see results | |
| pause and cancel | |
| manage access | |

No right implies another. Grants go to a person or to everyone who uses the
agent, for one workflow or for all of them, including new ones. "Check
someone's access" explains why a person may or may not do something. Earlier
grants keep working: "Run for me", "Run and control workflows", "See workflows
and runs" and "Schedule reports".

The same policy as text, applied with the revision you read:

```bash
hubzoid workflow-access export ./my-hub --output workflows/access.yaml
hubzoid workflow-access diff ./my-hub --file workflows/access.yaml
hubzoid workflow-access apply ./my-hub --file workflows/access.yaml --expected-revision 3
hubzoid workflow-access explain ./my-hub --subject ana@example.org --workflow weekly_report --action edit
```

Editing `workflows/access.yaml` changes nothing until it is applied. The API is
`/api/workflow-access`. A person can start a "Run for me" report over the API
with `POST /portal/api/my/runs` ([API](api.md#run-a-report-for-yourself)).

## Repeated requests

Ask in chat: "Repeat this for me every Monday at 9: list the stock items below
their reorder level."

1. The agent shows the exact request, when it runs and where answers go.
2. It saves the request only after you say yes.
3. A first run starts at once.
4. Each run asks the agent your request as you, with your current access, and
   puts the answer in a new private chat. It also shows in your notifications.

Times use your own time zone (Settings, Preferences) unless you name one.

Everyone who uses an agent may set up their own repeated requests. Manage them
in Settings, Schedules: run now, pause, resume, edit or stop. A hub turns the
feature off with `HUBZOID_PROMPT_WORKFLOWS=false`.

## Change the hub from chat

People with Save shared knowledge, and anyone who manages the agent, can ask
the agent to change its own instructions or knowledge. For example: "Add a
rule: orders over 5,000 need a second check."

1. The agent proposes exact edits to `AGENTS.md`, or to Markdown files under
   `knowledge/`, `skills/`, `agents/` or `prompts/`.
2. The proposal appears as a card with the diff.
3. Someone who manages the agent reviews it on the card, or in Settings, Hub
   changes, and applies or rejects it.
4. A change the agent proposes never applies itself, even for a manager.

Access, approval and run settings, workflow code, local tools, `.env`, secrets
and anything outside the hub are never proposable.

**Applying** writes exactly the reviewed text, and only if the files haven't
changed since the proposal. Otherwise it reports a conflict and writes nothing.
Instructions and skills take effect for new chat replies at once. Knowledge is
read live. Slack, MCP and other surfaces of a running agent pick up instruction
changes after a restart. Undo is a new proposal with its own review.

**From the CLI or API**, a change to `knowledge/` only, proposed by a manager
in an interactive terminal or their own browser session, applies at once.
Anything else waits for review, including any proposal sent with an API key.
Deciding a change needs a person's own session or terminal, never an API key,
whatever its operations.

```bash
hubzoid hub-changes list ./my-hub --status pending
hubzoid hub-changes show ./my-hub 3
hubzoid hub-changes propose ./my-hub --title "Friday hours" --edits edits.json
hubzoid hub-changes decide ./my-hub 3     # in your own terminal
hubzoid hub-changes undo ./my-hub 3
```

The API is `/api/hub-changes` (list, propose, show, `decision`, `undo`).

### Hub changes in Git

In a hub kept in Git, each applied change is one commit of those files:

| Part | Value |
|---|---|
| Author | The person who approved it |
| Committer | This checkout's own git identity, or `HUBZOID_GIT_COMMITTER="Name <email>"`, or Hubzoid |
| Message | The change's title plus `Hubzoid-Change`, `Hubzoid-Proposed-By` and `Hubzoid-Approved-By` trailers. The agent's reasoning stays in Hubzoid |

Agent instructions published from Settings are committed the same way.

Commits stay on the server unless the hub sets `HUBZOID_HUB_CHANGES_PUSH=1`.
Until then, Hub changes shows "committed on this server, not pushed yet". When
you deploy with `git pull` on the server, either set
`HUBZOID_HUB_CHANGES_PUSH=1` with a remote the server may push to, or push
from the hub folder after reviewing (`git push`) before the next pull. A push
is never forced or rebased. A refused push shows as needing attention.

## Memory

**Team memory.** Say "remember for the team: the South warehouse closes at 4pm
on Fridays". `remember` proposes an edit to `knowledge/_learned/<topic>.md`
through hub changes, and a manager applies it.

**Your own notes.** Say "remember that I prefer short bullet points". The agent
keeps one short note per person per agent, and only you see it. Each new chat
starts with your notes, and the agent's own instructions win when they
disagree.

- Settings, Memory shows, edits and deletes your notes. The API is
  `GET`, `PUT` and `DELETE /api/memory`.
- The agent changes them only in your own chat. Scheduled runs can't, and a
  page the agent reads can't.
- The agent never copies your notes into shared content.
