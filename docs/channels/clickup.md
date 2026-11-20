# ClickUp

Start work from a ClickUp task. Move the task to a status you choose (for
example "ready for agent"), or add a tag, and the agent or workflow works on
that task. Questions and results come back as comments on the task, and the
task's status can follow the work. See [Channels](../channels.md) for what
every provider shares.

## Example

`channels/clickup.yaml`:

```yaml
version: 1
id: clickup
provider: clickup
enabled: false
workflow: factory                     # or agent: main, for a chat answer on the task
run_as: linked_requester
provider_config:
  workspace_id: "9012345678"          # the number after app.clickup.com/ in the address
  list_ids: ["901234567890"]          # the lists work may start from (ids, not names)
  token_ref: secret:channel/company-hub/clickup/token
  trigger:
    status: ready for agent           # a task moved into this status starts work
  statuses:
    ack: in progress
    question: needs answer
    result: in review
  approve_in_task: true
```

The ids are placeholders.

## Settings

Under `provider_config`:

| Setting | Default | What it does |
|---|---|---|
| `workspace_id` | required | The ClickUp workspace id (digits), from `app.clickup.com/<id>/...`. |
| `list_ids` | required | The lists work may start from, 1 to 50 list ids (digits). List names are refused. |
| `token_ref` | required | Reference to a ClickUp personal API token (`pk_...`, from ClickUp Settings, Apps). |
| `trigger.status` | none | A task moved into this status starts work. |
| `trigger.tag` | none | A task that gets this tag starts work. Name a status, a tag or both. With both, both must hold. |
| `statuses.ack` | none | Status set when work starts, and again when progress follows an answer. |
| `statuses.question` | none | Status set when the work asks a question. |
| `statuses.approval` | `statuses.question` | Status set when a plan waits for approval. |
| `statuses.result` | none | Status set when the result is posted. |
| `statuses.error` | none | Status set when the work reports an error. |
| `approve_in_task` | `false` | The person who asked can approve a plan with a comment on the task. |

`run_as` must be `linked_requester`: work always runs as the person who moved
the task. Status names compare without case. An unset status leaves the task
where it is.

## Connect ClickUp

You need `HUBZOID_PUBLIC_URL` set to the hub's HTTPS address, and a ClickUp
user whose token can see every list in `list_ids`. Use a separate ClickUp user
for the hub where you can, so its comments are clearly the hub's.

With Claude Code or Codex:

> Connect this hub to our ClickUp workspace. Use workflow factory, start work
> when a task in our Requests list moves to "ready for agent", and move it to
> "needs answer" and "in review" as the work goes. Create the channel file,
> show the diff and verify with a test task. I will store the token myself.
> Do not change real tasks.

Or by hand. A person stores the token, never in a chat:

```sh
hubzoid channels apply ./company-hub --file channels/clickup.yaml --expected-revision absent
hubzoid secrets set ./company-hub --ref secret:channel/company-hub/clickup/token --stdin
hubzoid channels clickup setup ./company-hub clickup
hubzoid channels clickup members ./company-hub clickup      # ClickUp user ids
hubzoid channels link ./company-hub clickup --external-id 81234567 --account ana@example.org
hubzoid channels doctor ./company-hub clickup
hubzoid channels resume ./company-hub clickup --expected-revision <rev>
```

Then grant **Start work from clickup** (`channel_clickup`) under the agent's
**Access** in Settings to the people who may start work.

`setup` makes one webhook per list, pointed at
`$HUBZOID_PUBLIC_URL/api/hubs/<hub>/channels/clickup/webhook`, for task
created, status changed, tag changed and comment posted. ClickUp gives each
webhook its own secret. `setup` keeps it in the channel's secret slot and
never prints it. Run `setup` again after you add a list or change
`HUBZOID_PUBLIC_URL`: it keeps working webhooks, makes missing ones and
remakes stale ones.

ClickUp's API cannot add statuses. Add every status you name to each list (or
its space) in ClickUp yourself. `doctor` fails when a list lacks the trigger
status and warns for a missing `statuses` entry. Until you add it, the comment
is still posted and the task stays where it is.

## How it works

- **Who asked.** The person who moved the task, added the tag or created the
  task is the requester. They need an account link, access to the agent and
  the **Start work from clickup** grant. Anyone else gets one comment asking
  an administrator to link them, and no work starts.
- **Starting.** The task is read once before work starts. It must be in an
  allowed list and still in the trigger status (or still carry the tag). A
  task moved on before then starts nothing. A task created already in the
  trigger status or with the tag starts work. Other new tasks are skipped with
  no comment.
- **What the work sees.** The request is always "Please work on this ClickUp
  task." The task's name and description (up to 16,000 characters) go to the
  work as untrusted data, never as instructions.
- **Questions.** A question is posted as a comment, and the task moves to
  `statuses.question`. The next comment from the person who asked answers it.
  A number picks an option. Other comments while work runs are follow-ups the
  work reads at its next step.
- **Replies.** Replies are plain text comments, split into at most 10 parts.
  Files a workflow posts (such as a plan) are attached to the task. Each
  comment ends with a reference (`ref hz-...`). It lets the hub find a comment
  whose post timed out instead of posting it twice, and keeps the hub's own
  comments from starting work. A comment from the hub's ClickUp user without
  that reference counts as a person's.
- **Statuses.** The status for a reply is set after its last part is posted.
  A failed status change never undoes a comment. Moving the task after a pull
  request merges is left to people or to ClickUp's own GitHub integration.
- **No ClickUp tools.** The token is used only by the channel. Agents get no
  ClickUp tools, so unattended work never changes ClickUp on its own.

Filter the board by your question and approval statuses to see everything
waiting on people.

## Approve a plan from the task

With `approve_in_task: true`, a plan that needs approval is attached to the
task, the comment says what to reply, and the task moves to
`statuses.approval`. The person who asked replies with exactly one of:

| Reply | Effect |
|---|---|
| `approve` | Approves the plan. |
| `reject` | Rejects the plan. |
| `change: <what to change>` | Asks for changes. |

Anything else, such as "looks good", is an ordinary comment. Only plan
approvals settle this way. Any other approval links to the Inbox, and a plan
can always be approved there too. Who wrote the comment comes from ClickUp's
signed webhook, matched to the person's account link.

## Remove the webhooks

```sh
hubzoid channels clickup teardown ./company-hub clickup
```

`teardown` deletes the webhooks this connection made. Tasks stop starting
work. To stop work for a while but keep the webhooks, pause the channel
instead (`hubzoid channels pause`).

## Commands

| Command | What it does |
|---|---|
| `hubzoid channels clickup setup HUB ID` | Make or repair one webhook per list. Needs `HUBZOID_PUBLIC_URL`. |
| `hubzoid channels clickup members HUB ID` | List the workspace's people and their ClickUp user ids, for `hubzoid channels link`. |
| `hubzoid channels clickup teardown HUB ID` | Delete the webhooks this connection made. |

Each takes `--json`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `public_url_required` from `setup` | `HUBZOID_PUBLIC_URL` is not set | Set it to the hub's HTTPS address |
| Deliveries refused with `signature_invalid` | A webhook this connection did not make, or its secret was lost | Run `hubzoid channels clickup setup` again |
| `trigger_moved_on` in `hubzoid channels deliveries` | The task left the trigger status (or lost the tag) before work started | Move it back |
| `trigger_incomplete` | Both a status and a tag are named, and only one holds | Set both, or name only one in `trigger` |
| `list_not_allowed` | The task is in a list not in `list_ids` | Add the list, then run `setup` |
| `task_unavailable` | The token's user cannot read the task | Give that user access to the list |
| Doctor: `status_missing` | A list lacks a status the file names | Add it in ClickUp, or change the file |
| Doctor: `webhook_missing` or `webhook_unhealthy` | A webhook was deleted, disabled or points elsewhere | Run `setup` again |
| Doctor: `clickup:token` fails | ClickUp refused the token | Store the token again with `hubzoid secrets set` |
| `approve` did nothing | `approve_in_task` is off, someone else replied, or the comment had other words | Turn it on, ask the requester, or approve in the Inbox |
| "Ask an administrator to link your ClickUp account" | No account link for that ClickUp user | `hubzoid channels link` |
