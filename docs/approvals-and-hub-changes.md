# Approvals, questions, workflow access, hub changes and memory

This guide covers what 2.0 adds for people in the loop:

- the agent asking first
- workflows that wait for a person
- who may run which workflow
- "repeat this every Monday"
- changing the hub from chat
- the two kinds of memory

Each section gives the chat experience first, then the file, CLI and API forms.

## Approvals and questions

By default the agent asks nobody before using a tool. A hub chooses the tools that should ask in an optional `hitl.yaml` at the hub root:

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

The `ask` setting takes these values:

- `never` is the default.
- `once` asks the first time in a chat.
- `always` asks every time.
- `auto` lets a quick model check run routine calls and ask a person about anything risky.
- Connectors also take `writes`.
- `allow_always` adds "Always allow for me" to that tool's card.

A tool's own rule wins over its connector's.

**In chat.** When a tool must ask, the agent stops and a card appears under its message. The card offers:

- Approve
- Approve for this chat
- Always allow, when the hub allows it
- Reject with a note

"Exact action" shows the arguments exactly as they will run. Only the person who asked, or a named approver, can decide. An admin can't decide for them, and nor can an API key or the agent. After a decision, the same answer continues.

Writing a new message instead of deciding cancels the wait.

Questions work the same way: pick an option or type an answer.

**Waiting for you.** The sidebar's Waiting for you page lists every open request across your agents, with a count. Slack shows approvals as buttons in a private message. WhatsApp uses numbered replies. Email has a link to the page.

**CLI and API.**

```bash
hubzoid hitl validate ./my-hub --file hitl.yaml
hubzoid hitl export ./my-hub            # the active rules and their revision
hubzoid hitl apply ./my-hub --file hitl.yaml --expected-revision <revision from export>
hubzoid requests list ./my-hub --user ana@example.org
hubzoid requests decide ./my-hub <id>   # in your own terminal; asks you to retype a code
```

The API is `GET /api/requests`, `POST /api/requests/{id}/decisions` and `POST /api/requests/{id}/cancel`. Decisions are refused from API keys and scripts: a person decides in Hubzoid or in their own terminal.

## Workflows that wait for a person

A Python workflow can stop and wait. The run holds no process while it waits, and it continues after a restart:

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

- `hub.ask(question, options=None, approvers=None)` returns the answer.
- `hub.approve(action, details=None, approvers=None)` returns `{"approved", "note", "by"}`.
- `hub.review_plan(path, title=None, approvers=None)` publishes the exact plan. The person approves it or requests changes with a note.
  - A revised plan is a new version that needs its own approval.
  - Named approvers can open the plan.

**When you start a workflow from chat** ("Run Launch plan review for me"), its questions and approvals appear in that chat as cards, and also under Waiting for you. When the run ends after the chat stopped waiting, its result comes back to the same chat as a message. A run that nobody started from chat, such as a scheduled one, emails the person a link to Waiting for you.

## Who may run, schedule and change each workflow

Settings, Workflow access gives separate rights per workflow:

- run
- schedule (needs run too)
- edit
- create
- review changes
- see definition
- see runs
- see results
- pause and cancel
- manage access

No right implies another. Grants go to a person or to everyone who uses the agent, for one workflow or for all of them, including new ones. "Check someone's access" explains why a person may or may not do something.

Earlier grants keep working: "Run for me", "Run and control workflows", "See workflows and runs" and "Schedule reports".

The same policy as text, applied with the revision you read:

```bash
hubzoid workflow-access export ./my-hub --output workflows/access.yaml
hubzoid workflow-access diff ./my-hub --file workflows/access.yaml
hubzoid workflow-access apply ./my-hub --file workflows/access.yaml --expected-revision 3
hubzoid workflow-access explain ./my-hub --subject ana@example.org --workflow weekly_report --action edit
```

Editing `workflows/access.yaml` changes nothing until it is applied. The API is `/api/workflow-access`.

## Repeated requests

Ask in chat: "Repeat this for me every Monday at 9: list the stock items below their reorder level."

1. The agent shows the exact request, when it runs and where answers go.
2. It saves the request only after you say yes.
3. A first run starts at once.
4. Each run asks the agent your request as you, with your current access, and puts the answer in a new private chat. It also shows in your notifications.

Times use your own time zone (Settings, Preferences) unless you name one.

Everyone who uses an agent may set up their own repeated requests. Manage them in Settings, Schedules: run now, pause, resume, edit or stop. A hub turns the feature off with `HUBZOID_PROMPT_WORKFLOWS=false`.

## Changing the hub from chat

People with Save shared knowledge, and anyone who manages the agent, can ask the agent to change its own instructions or knowledge. For example: "Add a rule: orders over 5,000 need a second check."

1. The agent proposes exact edits to `AGENTS.md`, or to Markdown files under `knowledge/` or `skills/`.
2. The proposal appears as a card with the diff.
3. Someone who manages the agent reviews it on the card, or in Settings, Hub changes, and applies or rejects it.
4. A change the agent proposes never applies itself, even for a manager.

These are never proposable:

- access, approval and run settings
- workflow code and local tools
- `.env`
- anything that looks like a secret

**Applying** writes exactly the reviewed text, and only if the files haven't changed since the proposal. Otherwise it reports a conflict and writes nothing.

Instructions and skills take effect for new chat replies at once. Knowledge is read live. Slack, MCP and other surfaces of a running agent pick up instruction changes after a restart.

Undo is a new proposal with its own review.

**Git.** In a hub kept in Git, each applied change is one commit of those files:

- The **author** is the person who approved it.
- The **committer** is this checkout's own git identity, or `HUBZOID_GIT_COMMITTER="Name <email>"`, or Hubzoid.
- The **message** is the change's title plus `Hubzoid-Change`, `Hubzoid-Proposed-By` and `Hubzoid-Approved-By` trailers. The agent's reasoning stays in Hubzoid.
- Agent instructions published from Settings are committed the same way.

Commits stay on the server unless the hub sets `HUBZOID_HUB_CHANGES_PUSH=1`. Hub changes shows "committed on this server, not pushed yet" until then.

When you deploy with `git pull` on the server, either:

- set `HUBZOID_HUB_CHANGES_PUSH=1`, with a remote the server may push to, or
- push from the hub folder after reviewing (`git push`) before the next pull.

A push is never forced or rebased. A refused push shows as needing attention.

```bash
hubzoid hub-changes list ./my-hub --status pending
hubzoid hub-changes show ./my-hub 3
hubzoid hub-changes decide ./my-hub 3     # in your own terminal
hubzoid hub-changes undo ./my-hub 3
```

## Memory

**Team memory.** Say "remember for the team: the South warehouse closes at 4pm on Fridays". `remember` proposes an edit to `knowledge/_learned/<topic>.md` through hub changes, and a manager applies it.

**Your own notes.** Say "remember that I prefer short bullet points". The agent keeps one short note per person per agent, and only you see it. Each new chat starts with your notes, and the agent's own instructions win when they disagree.

- Settings, Memory shows, edits and deletes your notes.
- Only you, in a chat, can change them. Scheduled runs can't, and a page the agent reads can't.
- The agent never copies your notes into shared content.
