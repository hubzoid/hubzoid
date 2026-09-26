# Reports and email from workflows

A workflow generates a file, **publishes** it as a report that belongs to the
person the run acts as, and **emails** that person a link. Generating the file
is your code's job, whether that is HTML, PDF, CSV, an image or anything else.
Hubzoid stores it, shows it, controls who can open it and sends the email.

```python
from hubzoid import hub, workflow

@workflow(schedule="every day at 8am", timezone="Asia/Kolkata")
def daily_report():
    page = hub.run_dir / "report.html"          # a private folder for this run
    page.write_text(build_html())               # your own generator
    report = hub.publish_artifact(page, title="Daily report")
    hub.send_email("Your daily report", "It is ready.", artifacts=[report])
```

A complete example, with a reusable report template (typography, tables, a
chart, print styling), is in [examples/report](examples/report/). Nothing about
the template is required: publish any HTML you like.

## Publishing

`hub.publish_artifact(path, *, title=None, audience="owner", share_with=())`
returns `{"id", "url", "title", "filename", "content_type", "size"}`.

- **Owner.** The owner is the account the run acts as
  ([workflow-identity.md](workflow-identity.md)). No caller can choose it.
- **Private by default.**
  - `audience="hub"` shares the report with everyone who can use the agent.
  - `audience="people", share_with=["sam@company.com", {"kind": "group",
    "principal": "finance"}]` shares it with those people or groups.
  - Both need a Console-managed hub.
  - A workflow can never create a public link.
- **Every call stores a new report.** Earlier reports are never overwritten,
  even when the file name is the same.
- **Recorded server-side:** owner, hub, workflow, run, content type, size,
  SHA-256, storage location, creation time and sharing.
- **Stored privately** under `<hub>/.hubzoid/artifacts/`. Agent file tools
  cannot read that folder, the older chat download links (`/artifacts/...`)
  cannot reach it, and `hubzoid backup` includes it.
- **Recovery-safe.** Publishing is a recorded step, so a run that resumes after
  a crash returns the report it already published.
- **Size limit.** The largest file is `HUBZOID_ARTIFACT_MAX_BYTES` (default
  50 MiB).

Markdown tasks can publish too, when the task opts in (see
[Markdown tasks](#markdown-tasks)).

## Viewing

`url` is `https://<your host>/portal/artifacts/<id>`. The page has a thin
toolbar with the title, creation time, **Share** (owner only) and
**Download**, and the report below it.

| File | Shown as |
|---|---|
| `.html` | The page itself, in an isolated frame |
| `.pdf` | The browser's PDF viewer |
| `.png` `.jpg` `.gif` `.webp` `.svg` | An image |
| `.csv` `.tsv` | A table (the first 500 rows) |
| `.txt` `.md` `.json` `.log` `.yaml` | Plain text |
| anything else | A **Download** button only. It is never opened in the page. |

**Signing in.**

- A signed-in person with access sees the report at once.
- A signed-out person is sent to the normal sign-in, with password or Google,
  and comes back to the same report. The return address is built by Hubzoid
  from the report id and is never taken from the link.
- Nobody needs Admin Console rights to view or share their own reports.
- A link in WhatsApp or email is not a sign-in: the person still signs in in
  the browser.

**Isolation.**

- **HTML reports run apart from the app.** They load with a sandbox that gives
  them their own opaque origin. Report scripts cannot read the session, call
  the app with it, or touch the toolbar.
- **HTML reports make no network requests by default.** They cannot load or
  send anything elsewhere. To let reports load a chart library from a CDN, set
  `HUBZOID_ARTIFACT_ALLOW_ORIGINS=https://cdn.jsdelivr.net`.
- **Other types.** SVG files are shown as images, so their scripts do not run.
  Types without a preview only download.

## Sharing

The owner is the chat-app account the report was published under. It is not
just the email address.
- **A replacement account inherits nothing.** A new account that reuses the
  email gets none of the old account's reports or the shares made to it.
- **The owner must be active.** A blocked owner loses access to their reports
  until they are reactivated.
- **On a Console-managed hub, the owner also needs Use this agent.** This
  applies to their reports, public links, sharing and deletion, and access
  returns when the permission does.

The owner chooses one audience per report in **Share**:

| Audience | Who can open it |
|---|---|
| **Only you** (default) | The owner. |
| **Specific people or groups** | Named accounts, or members of a chat-app or roster group. Each must currently be able to use the agent. |
| **Anyone with access to this agent** | Everyone who can currently use the agent. |
| **Anyone with the link** | Anyone holding the link, with no sign-in. |

- **Access is decided on every request.** Removing someone from the list, from
  the group or from the agent takes effect on their next click. So does
  blocking their account.
- **Changing one report never changes future reports.**
- **Sharing with people or with the agent needs a Console-managed hub.** A
  legacy hub's membership lives in the chat app, and Hubzoid cannot check it.
- **Owner and viewers.** The owner can view, download, share, turn off links
  and delete. Viewers can view and download. There is no editor role.
- **No administrator override.** Organization admins and agent managers see
  nobody's private reports.

### Public links

**Anyone with the link** needs its own permission in the agent: **Share reports
by public link** (`share_public_links`). Publishing never grants it.

- **When it is checked.** It is checked when the owner creates the link, and
  again every time the link is opened. Revoking the permission (or blocking
  the owner) turns off the owner's live links for good: granting it again does
  not bring them back. The owner creates a new link instead.
- **A link that stops working** opens a "Link not available" page with a way
  to sign in, never an endless "Loading…".
- **Lifetime.** A link expires after 1, 7, 30 or 90 days. The default is
  `HUBZOID_ARTIFACT_LINK_DAYS`, which is 7.
- **Rotating and turning off.** **Create new link** replaces the old one, which
  stops working at once. **Turn off link** ends it.
- **The owner sees the link once.** Hubzoid keeps only a hash of it.
- **The secret stays out of logs.** It travels after `#` in the address
  (`/portal/p/#…`), so browsers never send it in a request, and it never appears
  in server logs or referrers. The page exchanges it for a 15-minute cookie
  scoped to the report pages.
- **Clear warning in the dialog.** Anyone who has the link can open the report
  without signing in.

## Email

`hub.send_email(subject, body="", *, artifacts=(), raise_on_failure=True)`

- **The recipient is fixed.** It is always the run's own account email. There is
  no `to`, `cc` or `bcc`, so neither code nor a model can address anyone else.
- **Report links.** `artifacts` takes reports the same account owns. The email
  links to them, and opening a link needs a sign-in. The email has no
  attachments.
- **Return value.** It returns the delivery result. Unless the result is
  `accepted` or `previewed`, it raises `hubzoid.email_delivery.EmailError`. With
  `raise_on_failure=False` it returns the result instead.
- **No permission needed.** Calling `send_email` in a workflow's own code is the
  delivery setting, and the recipient needs no Gmail connection to receive it.

### Configuration

Set these once for the deployment (the gateway's environment or deployment
secret). A hub can override them in its `.env` or hub secret.

| Setting | Default | |
|---|---|---|
| `HUBZOID_SMTP_HOST` | none | Required for sending. |
| `HUBZOID_SMTP_PORT` | 587 (465 with SSL) | |
| `HUBZOID_SMTP_FROM` | none | Required: the sender address. |
| `HUBZOID_SMTP_USERNAME`, `HUBZOID_SMTP_PASSWORD` | none | Sent only over TLS. |
| `HUBZOID_SMTP_STARTTLS` | true | Certificates and host names are verified. |
| `HUBZOID_SMTP_SSL` | false | Implicit TLS. |
| `HUBZOID_SMTP_TIMEOUT` | 30 | Seconds per SMTP operation. |
| `HUBZOID_EMAIL_DELIVERY` | `smtp` | `preview` writes each email to `<hub>/.hubzoid/outbox/<person>/` and sends nothing. |

The SMTP credentials stay out of workflow code, agent processes, run results
and logs. The email is refused, never reported as sent, when:

- email is not configured;
- the recipient cannot receive mail (`admin@localhost`, a `.local` address, a
  single-word domain);
- the account is blocked or awaiting approval.

Locally, use `HUBZOID_EMAIL_DELIVERY=preview`. The result says plainly that
nothing was sent and where the `.eml` file is.

### What "sent" means, and retries

SMTP cannot promise exactly-once delivery, and Hubzoid does not claim it.

| Result | Meaning |
|---|---|
| `accepted` | The SMTP server accepted the message for delivery. Delivery to the inbox is not confirmed. |
| `previewed` | Written to the outbox. Not sent. |
| `failed` | Not sent: refused before the message was transferred, or rejected by the server. |
| `ambiguous` | The connection ended while the server was receiving the message. It may or may not have been accepted. |
| `refused` | Hubzoid did not try: configuration, recipient, account or input problem. |

Each email is a recorded step with a delivery record (`hz_email_deliveries`):

- **Failures before the message is transferred are retried** a few times. Such
  a failure could be a connection error, or a temporary refusal of the sender or
  recipient. Nothing could have been accepted, so a retry is safe.
- **An `ambiguous` send is never retried automatically.** Check the inbox and
  send again only if it did not arrive.
- **A run that resumes after the server accepted the message does not send it
  again.**
- **Duplicates are recognizable.** Each message's `Message-ID` is derived from
  its delivery record.

## Markdown tasks

A markdown task can publish and email when its file opts in:

```markdown
---
schedule: "0 7 * * 1-5"
run_as: priya@company.com
publish_artifacts: true
send_email: true
---
Write today's digest to the scratch folder named in your instructions, publish
it, and email me a link.
```

- **Tools.** The agent gets `publish_artifact(path, title)` and
  `send_email(subject, body, artifact_ids)`.
- **What it can publish.** Only files under the task's writable paths.
- **Recipient and owner are fixed.** The owner and the recipient are the account
  the task runs as, and the model can change neither.
- **Limits.** A run can send at most 5 emails.
- **Existing tasks.** Tasks without these keys are offered nothing new.
- **Runtimes.** Both tools behave the same on every runtime.
