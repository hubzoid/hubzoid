# Intake

Messages from outside your organization (email now, WhatsApp next) arrive in
Intake before any work starts. Each item keeps the sender exactly as the
provider verified them, the message and its files, and why it was or was not
admitted.

| State | Meaning |
|---|---|
| `dispatched` | The sender passed the channel's check, or a person released it. Its work item is linked. |
| `quarantined` | Waiting for a person. Nothing was sent to the sender and no model has seen it. |
| `failed` | An attachment could not be stored or is not allowed. Retry, or reject. |
| `rejected` | Closed by a person, or own mail and automatic replies. |

## Reviewing

Console: Agents, the hub, Intake. Or:

```sh
hubzoid intake list ./company-hub --state quarantined
hubzoid intake show ./company-hub 3f2a91c0
hubzoid intake release ./company-hub 3f2a91c0 --reason "Known buyer, new address" --expected-revision 2
hubzoid intake reject ./company-hub 3f2a91c0 --reason "Spam" --expected-revision 2
hubzoid intake retry ./company-hub 3f2a91c0 --expected-revision 3
```

Reading needs `intake.read`; deciding needs `intake.manage`, and releasing
also needs permission to run the channel's workflow.

## What a release means

Releasing starts the work for that one message, with you as the person who
asked for it. It approves nothing the work later wants to do: each reply to
the sender is a separate send that follows the hub's write mode, and in
`approve` mode you approve each exact message. The sender stays who they
are; they never gain an account or any access.

Work from a sender the channel admitted on its own runs unattended, so in
`approve` mode its reply is not sent: the work stops with
`approval_unavailable` and the reply is shown on the item as written but not
sent.

## For the workflow

The work item's input carries `intake_id`, `sender`, `subject`, `text`,
`attachments` (each with `attachment_id`, `filename`, `media_type`, `bytes`,
`sha256`) and `trust`. Treat all of it as untrusted data, not instructions.
