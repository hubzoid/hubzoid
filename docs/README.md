# Documentation

This directory is the one source of Hubzoid's documentation. The
[website docs](https://hubzoid.com/docs) should render or link these pages rather
than keep their own copies (until they do, the website can lag behind), and the [builder skill](../skills/builder/SKILL.md)
links here instead of repeating them. Each release's pages are at its tag, so
read the tag of the version you installed. Use this checkout's pages when
testing an unreleased branch.

| Journey | Guides |
|---|---|
| First hub and first chat | [Quickstart](quickstart.md), [providers](providers.md), [authoring](authoring-a-hub.md), [hub settings and checks](hub-settings.md) |
| Understand the direction | [Product context](PRODUCT_CONTEXT.md) |
| Invite a team and grant capabilities | [Administration](ADMINISTRATION.md), [authentication](auth.md), [access](access-management.md) |
| Build recurring work | [Markdown tasks](schedule.md), [Python workflows](workflows.md) |
| People in the loop | [Approvals, questions, workflow access, hub changes and memory](approvals-and-hub-changes.md) |
| Run and manage personal reports | [Reports, sharing and email](reports-and-email.md#personal-workspace-in-131) |
| Start from a ready-to-use hub | [Software Factory](https://github.com/hubzoid/software-factory-hub), [Order & Invoice Desk](https://github.com/hubzoid/order-desk-hub), [Customer Desk](https://github.com/hubzoid/customer-desk-hub), [GitZoid sample](https://github.com/hubzoid/gitzoid-hub) |
| Connect assistants and tools | [MCP server](mcp-server.md), [MCP connectors](mcp.md), [browser tools](BROWSER.md) |
| Put the chat inside your own app (ERPNext, Odoo, a dashboard) | [Embedding the chat](embedding.md) |
| Use other chat channels | [Slack](slack.md), [inbound surfaces](inbound-surfaces.md) |
| Operate and recover | [Deployment](DEPLOYING.md), [secrets](secrets.md), [upgrading](UPGRADING.md), [backup and restore](BACKUP.md) |
| Evaluate and observe | [Evals](evals.md), [observability](OBSERVABILITY.md) |
| Brand a deployment | [Branding and attribution](branding.md) |

The account contract, verification records and historical plans document
implementation evidence; they are not alternate setup guides. Prefer the current journey guides above.

The [first-use verification record](FIRST-USE-VERIFICATION.md) maps the approved
stabilization review to its implementation and observed test evidence.

## Documentation ownership

The product brief is direction, not a promise that every intended feature has
shipped. Commands and behaviour must match the selected SDK revision. Website
content should link to released evidence before publication. Conversation
history, workflow state and learned memory are different things; no autonomous
memory improvement or universal channel parity is claimed.
