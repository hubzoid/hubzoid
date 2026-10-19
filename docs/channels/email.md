# Email

A shared mailbox (orders@, support@) becomes a source of work. New mail is
read from the moment you start, checked, and either starts the configured
workflow or waits for a person in [Intake](../intake.md). Replies go only to
the person who wrote, in the same conversation, and only as your hub's write
mode for `email.send` allows. See [Channels](../channels.md) for what every
provider shares.

## The file

```yaml
version: 1
id: orders-email
provider: email
enabled: false
workflow: orders            # the work that starts for each message
run_as: workflow            # email senders are outside: the work acts as the workflow
provider_config:
  transport: imap           # imap, gmail or microsoft365
  mailbox: orders@example.com
  poll_seconds: 60
  imap:
    host: imap.example.com
    port: 993
    tls: implicit           # or starttls; there is no plain-text mode
    username: orders@example.com
    password_ref: secret:channel/company-hub/orders-email/imap-password
    drafts_folder: Drafts   # optional; found from the server when left out
  smtp:
    host: smtp.example.com
    port: 465
    tls: implicit
    username: orders@example.com
    password_ref: secret:channel/company-hub/orders-email/imap-password
  sender_policy:
    allowed: [buyer@customer.example]   # exact addresses, no wildcards
    authentication: aligned_dkim        # or manual: every message waits for a person
  attachments:
    allowed_types: [application/pdf, text/csv, image/png, image/jpeg]
    max_count: 10
    max_each_bytes: 10485760
    max_total_bytes: 20971520
```

Gmail and Microsoft 365 use the customer's own OAuth app instead of a
password:

```yaml
  transport: gmail
  gmail:
    client_id: 1234.apps.googleusercontent.com
    client_secret_ref: secret:channel/company-hub/orders-email/google-client-secret
    oauth_ref: secret:channel/company-hub/orders-email/gmail-oauth   # written by connect
    label_ids: [INBOX]
```

```yaml
  transport: microsoft365
  microsoft365:
    app_id: 00000000-0000-0000-0000-000000000000    # your Entra app
    tenant_id: example.onmicrosoft.com
    client_secret_ref: secret:channel/company-hub/orders-email/m365-client-secret
    oauth_ref: secret:channel/company-hub/orders-email/m365-oauth
```

## Setting it up

> Connect our orders@ mailbox to the orders workflow. Admit only
> buyer@customer.example with a verified signature, keep everything else for
> review, and leave replies off. Create the channel file, show the diff, run
> doctor, then start reading new mail. Do not send anything.

```sh
hubzoid secrets set ./company-hub --ref secret:channel/company-hub/orders-email/imap-password --stdin --apply
hubzoid channels apply ./company-hub --file channels/orders-email.yaml --expected-revision absent
hubzoid channels doctor ./company-hub orders-email
hubzoid channels resume ./company-hub orders-email
hubzoid channels email poll ./company-hub orders-email    # the first run only records where to start
```

For Gmail or Microsoft 365, set `HUBZOID_PUBLIC_URL`, add
`<public url>/api/hubs/<hub>/channels/<id>/email/oauth/callback` as the
redirect address of your OAuth app, then run
`hubzoid channels email connect ./company-hub orders-email` and open the link
signed in as the mailbox. Consent for any other mailbox is refused.

| Provider | Permissions asked for |
|---|---|
| Gmail | `gmail.readonly` to read. `--mode approve` adds `gmail.send`; `--mode draft` adds `gmail.compose` (which can also send at Google, but Hubzoid still sends only what a person approved). |
| Microsoft 365 | `offline_access`, `User.Read`, `Mail.ReadWrite` (read and drafts), `Mail.Send`. |

## Who gets in

- Only mail that arrives after you start is read. Nothing is marked read or
  moved in the mailbox.
- A message starts work when it has exactly one From address, that address
  is on `allowed`, and it carries a valid DKIM signature from the same domain
  covering From and Subject (not rsa-sha1, no body-length limit). DKIM shows
  the domain sent it, not who the person is, so the work always runs as the
  workflow.
- Anything else waits in Intake with its reason: `unknown_sender`,
  `sender_unverified`, `dkim_unavailable` (DNS did not answer), or a
  structural problem. Your own mail and automatic replies are recorded and
  never become work.
- An attachment of a type you did not allow, or one that cannot be stored,
  holds the whole message (`attachment_failed`). Files never go missing from
  an order silently.
- The same message seen twice (a redelivery, or the server renumbering its
  folder) is one item.

## Replies

A workflow answers with `hub.thread.post(text, key="answer")`. Every email
reply performs `email.send`, so the route must allow it
(`Route(actions=("email.send",))`) and `work-items.yaml` decides:

| `email.send` | What happens |
|---|---|
| `off` (the default) | Nothing is sent or saved. |
| `draft` | A real draft is saved in the mailbox for a person to send there. First run `hubzoid channels email rehearse-draft`, which saves one draft to the mailbox itself and reads it back. Until then drafts are refused with `draft_unsupported`. |
| `approve` | The exact message waits in Operations for the work's requester to approve, then it is sent once. |

The recipient is always the admitted sender, in the same conversation
(In-Reply-To and References). A Reply-To header, a cc or anything in the text
cannot change it. A send whose outcome is unknown (the connection dropped
after the message went) is never repeated on its own; a person resolves it.

Who approves: when a person releases a message from Intake, they asked for
that work, so they approve its replies. Work from an admitted sender runs
unattended, as the workflow, so nobody can approve its replies. Use `draft`
for such a channel. In `approve` mode an unattended reply is not sent and
does not wait: the work stops with `approval_unavailable`, and the reply is
kept on the Intake item for a person to read.

## Limits

- Polling, not push: new mail is seen within `poll_seconds`.
- At most 20 messages per pass; the rest come on the next pass.
- Messages over 30 MiB, more than 256 MIME parts or nesting deeper than 10
  are kept for review, not parsed further.
- The message source and its attachments count against the intake storage
  allowance of the hub's file service.
- IMAP with OAuth (other than Gmail's API) is not supported; use an app
  password over TLS.
- Tested here against fake IMAP, SMTP, Gmail and Graph servers only. Check a
  real mailbox with a disposable account before relying on it.

## Troubleshooting

| Code | What to do |
|---|---|
| `credential_missing` | Set the password or client secret with `hubzoid secrets set`. |
| `mailbox_mismatch` | Consent was given by another account. Run connect again, signed in as the mailbox. |
| `auth_revoked` | The provider refused the token renewal. Connect again. |
| `draft_unsupported` | Run `rehearse-draft`, set `imap.drafts_folder`, or use `approve`. |
| `sender_unverified` | The sender's domain did not sign the message. Release it in Intake if you know it is genuine, or use `authentication: manual`. |
| `provider_override` | `HUBZOID_GMAIL_API_URL`, `HUBZOID_GRAPH_API_URL` or `HUBZOID_EMAIL_CA_FILE` is set. Use these only with a local sandbox. |
