# Documentation

The [website docs](https://hubzoid.com/docs) organize the product guides for readers.
This directory retains revision-specific guides for source users and reviewers.
Website publication is independent of this checkout; use these local guides when
testing an unreleased branch.

| Journey | Guides |
|---|---|
| First hub and first chat | [Quickstart](quickstart.md), [providers](providers.md), [authoring](authoring-a-hub.md) |
| Understand the direction | [Product context](PRODUCT_CONTEXT.md) |
| Invite a team and grant capabilities | [Administration](ADMINISTRATION.md), [authentication](auth.md), [access](access-management.md) |
| Build recurring work | [Markdown tasks](schedule.md), [Python workflows](workflows.md) |
| Connect assistants and tools | [MCP server](mcp-server.md), [MCP connectors](mcp.md), [browser tools](BROWSER.md) |
| Use other chat channels | [Slack](slack.md), [inbound surfaces](inbound-surfaces.md) |
| Operate and recover | [Deployment](DEPLOYING.md), [upgrading](UPGRADING.md), [backup and restore](BACKUP.md) |
| Evaluate and observe | [Evals](evals.md), [observability](OBSERVABILITY.md) |
| Brand a deployment | [Branding and attribution](branding.md) |

[Legacy access](legacy-access.md) is for unmigrated hubs only. The account contract,
verification records and historical plans document implementation evidence; they
are not alternate setup guides. Prefer the current journey guides above.

The [first-use verification record](FIRST-USE-VERIFICATION.md) maps the approved
stabilization review to its implementation and observed test evidence.

## Documentation ownership

The product brief is direction, not a promise that every intended feature has
shipped. Commands and behaviour must match the selected SDK revision. Website
content should link to released evidence before publication. Conversation
history, workflow state and learned memory are different things; no autonomous
memory improvement or universal channel parity is claimed.
