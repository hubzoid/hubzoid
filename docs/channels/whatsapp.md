# WhatsApp

Your company's own WhatsApp Business number on the Meta Cloud API becomes a
source of work. Each message arrives in [Intake](../intake.md) with its
media and waits for a person. Replies go to the same sender only after the
person who released the message approves the exact text, and only within
WhatsApp's 24-hour window. There are no automatic replies. See
[Channels](../channels.md) for what every provider shares.

## The file

```yaml
version: 1
id: whatsapp-desk
provider: whatsapp_cloud
enabled: false
workflow: receive-request   # the work that starts when a person releases a message
run_as: workflow            # senders are outside the organization
provider_config:
  waba_id: "100000000000001"          # your WhatsApp Business Account id
  phone_number_id: "100000000000002"  # the number's id (not the phone number)
  graph_version: v22.0
  app_secret_ref: secret:channel/company-hub/whatsapp-desk/app-secret
  verify_token_ref: secret:channel/company-hub/whatsapp-desk/verify-token
  access_token_ref: secret:channel/company-hub/whatsapp-desk/access-token
  unknown_sender: quarantine          # or reject: kept as evidence, never work
  media:
    max_bytes: 20971520
    allowed_types: [application/pdf, image/jpeg, image/png]
  replies:
    mark_read: false                  # blue ticks when a message is received
    outside_window: hold
```

The ids above are placeholders.

## Setting it up

> Connect our WhatsApp Business number to the receive-request workflow.
> Keep every message for review, allow PDF and images, and leave replies off.
> Create the channel file, show the diff, set the secrets and run doctor.
> Do not send anything.

```sh
hubzoid secrets set ./company-hub --ref secret:channel/company-hub/whatsapp-desk/app-secret --stdin --apply
hubzoid secrets set ./company-hub --ref secret:channel/company-hub/whatsapp-desk/verify-token --stdin --apply
hubzoid secrets set ./company-hub --ref secret:channel/company-hub/whatsapp-desk/access-token --stdin --apply
hubzoid channels apply ./company-hub --file channels/whatsapp-desk.yaml --expected-revision absent
hubzoid channels doctor ./company-hub whatsapp-desk
```

In your Meta app (developers.facebook.com, WhatsApp, Configuration), set the
callback URL to the `webhook_url` doctor prints
(`<HUBZOID_PUBLIC_URL>/api/hubs/<hub>/channels/whatsapp-desk/webhook`), enter
the same verify token and subscribe to `messages`. Use a system user access
token with `whatsapp_business_messaging` for this number. Then
`hubzoid channels resume ./company-hub whatsapp-desk`.

A hub that already uses the older `WHATSAPP_*` settings keeps working until a
channel file serves the same number; from then on only the channel answers.

## How it behaves

- Meta's signature over the exact body is checked and the delivery is saved
  before Meta gets its 200, so a restart never loses a message, and a
  redelivered message is one item.
- Every message waits in Intake (`unknown_sender: quarantine`) or is closed
  with its evidence (`reject`). Nothing is sent to the sender and no model
  sees it until a person releases it.
- Media is fetched by its id from Meta's own media hosts only, with no
  redirects, then checked against `allowed_types` and `max_bytes`.
- Delivery receipts (sent, delivered, read, failed) never start work. The
  newest status of each reply is shown next to it.

## Replies

A workflow answers with `hub.thread.post(text, key="answer")`. Every reply
performs `customer_reply.send`, so the route must allow it and
`work-items.yaml` decides. WhatsApp has no drafts, so the mode for this
channel is `off` or `approve`:

```yaml
version: 1
write_modes:
  customer_reply.send: "off"
write_mode_overrides:
  whatsapp-desk:
    customer_reply.send: approve
```

- `approve`: the exact message waits in Operations for the person who
  released the message, then it is sent once.
- Outside 24 hours from the sender's latest message, a reply is held with
  `window_closed`, never sent as anything else. Retry it in Operations after
  they write again. Message templates are not supported yet.
- Progress updates are never sent to a sender.
- A send whose outcome is unknown (Meta did not answer) is never repeated on
  its own; a person resolves it.

## Limits

- Tested here against a fake Meta API only. Check with a test number before
  relying on it.
- `graph_version` must be one this release lists (v22.0 to v25.0); check
  Meta's current versions before going live.
- No templates, so a business cannot start a conversation.

## Troubleshooting

| Code | What to do |
|---|---|
| `signature_invalid` | The app secret or verify token differs from Meta's. Set it again. |
| `phone_number_mismatch` | Meta sent a message for another number of the same app. It was recorded and ignored. |
| `draft_unsupported` | Set `write_mode_overrides.<id>.customer_reply.send` to `off` or `approve`. |
| `window_closed` | The sender has not written for 24 hours. Wait for their next message, then retry. |
| `attachment_blocked` | Meta returned a media link on an unexpected host. Nothing was fetched. |
