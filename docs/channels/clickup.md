# ClickUp

Move a ClickUp task to a status you choose (for example "ready for agent"), or
add a tag, and the work happens on that task. Questions and results come back
as comments on the task, a comment from the person who asked answers the
question the work waits on, and the task's status follows the work when you
name statuses. See [Channels](../channels.md) for what every provider shares.

## The file

```yaml
version: 1
id: clickup
provider: clickup
enabled: false
workflow: factory            # or agent: main for a chat answer on the task
run_as: linked_requester
provider_config:
  workspace_id: "9012345678"          # the number after app.clickup.com/ in the address
  list_ids: ["901234567890"]          # the lists work may start from (digits, not names)
  token_ref: secret:channel/company-hub/clickup/token
  trigger:
    status: ready for agent           # a task moved into this status starts work
    # tag: hubzoid                    # or a tag added to the task (both named: both must hold)
  statuses:                           # optional: where the task moves as the work goes
    ack: in progress                  # work started, and again when it moves on after an answer
    question: needs answer            # the work asked a question in a comment
    result: in review                 # the result (a draft pull request) was posted
    # error: blocked
```

The ids above are placeholders. Every status you name must exist in each list
(doctor checks). Statuses compare without case.

## Setting it up

> Connect this hub to our ClickUp workspace. Use workflow factory, start work
> when a task in our Requests list moves to "ready for agent", and move it to
> "needs answer" and "in review" as the work goes. Create the channel file,
> show the diff, store the token, make the webhooks and verify with a test
> task. Do not change real tasks.

The token is a ClickUp personal API token (`pk_…`, Settings, Apps). Use a
separate ClickUp user for the hub where you can, so its comments are clearly
the hub's. A person stores it, never in the chat:

```sh
hubzoid channels apply ./company-hub --file channels/clickup.yaml --expected-revision absent
hubzoid secrets set ./company-hub --ref secret:channel/company-hub/clickup/token --stdin
hubzoid channels clickup setup ./company-hub clickup
```

`setup` makes one webhook per list, pointed at
`$HUBZOID_PUBLIC_URL/api/hubs/<hub>/channels/clickup/webhook`, for new tasks,
task status changes, tag changes and comments. A task made already carrying the
tag (or already in the trigger status) starts work too, and other new tasks are
skipped without a comment. ClickUp gives every webhook its own secret;
`setup` keeps each in the channel's secret slot and never prints it. Run it
again after you add a list or change `HUBZOID_PUBLIC_URL`: it keeps working
webhooks, makes missing ones and remakes stale ones (including webhooks from an
earlier version that miss an event). `teardown` deletes them.

Then link people and check:

```sh
hubzoid channels clickup members ./company-hub clickup      # ClickUp user ids
hubzoid channels link ./company-hub clickup --external-id 81234567 --account ana@example.org
hubzoid channels doctor ./company-hub clickup
hubzoid channels resume ./company-hub clickup --expected-revision <rev>   # the file ships paused
```

Who may start work is a grant: "Start work from clickup" under the agent's
Access in Settings.

## How it behaves

- **Who asked.** The person who moved the task (or added the tag) is the
  requester. They need an account link, access to the hub and the grant.
  Anyone else gets one comment asking an administrator to link them, and no
  work starts.
- **What the work sees.** The task's name and description go to the work as
  untrusted data, never as instructions. The request itself is fixed: "Please
  work on this ClickUp task."
- **The task is read once at the start.** It must be in an allowed list and
  still in the trigger status (or still carry the tag). A task moved on before
  the work starts starts nothing.
- **Questions and answers.** When the work asks a question, it is posted as a
  comment (and the task moves to `statuses.question`). The next comment from
  the person who asked answers it, and the work continues. A number picks an
  option. Approvals are never decided from a comment: the comment links to the
  Inbox. Other comments while work runs are follow-ups the work reads at its
  next step.
- **Replies.** Each comment ends with a short reference (`ref hz-…`). It lets
  the hub find a comment whose post timed out instead of posting it twice, and
  keeps its own comments from starting work. A comment from the hub's ClickUp
  user without that reference is a person's and counts.
- **Statuses.** A failed status change never undoes a comment. Moving the task
  after the pull request merges is left to people or to ClickUp's own GitHub
  integration.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Deliveries refused with `signature_invalid` | A webhook this connection did not make, or its secret was lost | `hubzoid channels clickup setup` again |
| `trigger_moved_on` in the deliveries list | The task left the trigger status before the work started | Move it back |
| `list_not_allowed` | The task is in a list not in `list_ids` | Add the list, then run `setup` |
| Doctor: no status "…" | The list lacks a status the file names | Add it in ClickUp, or change the file |
| "Ask an administrator to link your clickup account" | No account link for that ClickUp user | `hubzoid channels link` |
