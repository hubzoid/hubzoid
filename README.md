<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/hubzoid/hubzoid/72ac7bf1610c8a335b9b3ac3b536de8fdb6770f1/assets/mark-dark.svg">
    <img alt="Hubzoid" src="https://raw.githubusercontent.com/hubzoid/hubzoid/72ac7bf1610c8a335b9b3ac3b536de8fdb6770f1/assets/mark-light.svg" width="240">
  </picture>
</p>

<h3 align="center">One brain for your internal agents.</h3>
<p align="center">Build agents around your knowledge and your way of working. Start on your laptop. Bring them to your team.</p>
<p align="center">
  <a href="https://hubzoid.com/docs">Documentation</a> ·
  <a href="https://github.com/hubzoid/hubzoid/blob/main/docs/quickstart.md">Quickstart</a> ·
  <a href="https://hubzoid.com">Website</a> ·
  <a href="https://github.com/hubzoid/hubzoid/blob/main/CONTRIBUTING.md">Contribute</a>
</p>
<p align="center">
  <a href="https://pypi.org/project/hubzoid/"><img src="https://img.shields.io/pypi/v/hubzoid?color=B5471F" alt="PyPI version"></a>
  <a href="https://github.com/hubzoid/hubzoid/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-0B0B0C" alt="Apache License 2.0"></a>
</p>

**Hubzoid turns a folder of your knowledge, rules and tools into an agent you can
use yourself and share with your team.** Ask it questions in chat, run repeatable
workflows, or connect your own assistant through MCP. The rules live in files you
can read, edit and version.

<p align="center">
  <img alt="Your agent folder becomes a shared Hub for chat, workflows and your assistant through MCP." src="https://raw.githubusercontent.com/hubzoid/hubzoid/72ac7bf1610c8a335b9b3ac3b536de8fdb6770f1/assets/shared-hub-light.svg" width="680">
</p>

## Start in two minutes

You need Python 3.11 or 3.12, and Claude Code or Codex signed in on this
machine (or a model API key).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install hubzoid
hubzoid init my-hub
hubzoid run my-hub
```

The web app opens at **http://127.0.0.1:3080**. Ask:

> A customer wants a refund 40 days after delivery. What can we offer?

`my-hub` is a small starter for Kestrel & Oak, a fictional shop. It has every
folder Hubzoid reads, three house rules in `knowledge/house-rules.md`, and a
check in `evals/`. The assistant answers from the rules and names the file.

`init` uses the Claude Code or Codex you are signed in to. Without one, it offers
to save a model API key in the hub's private `.env`. Subscription limits or
provider charges apply. [Choose a runtime](https://github.com/hubzoid/hubzoid/blob/main/docs/providers.md)

## Make it yours

1. Open `my-hub/knowledge/house-rules.md` and change the refund window from 30
   days to 45. Or ask Claude Code or Codex to make the change and show you the diff.
2. Stop `hubzoid run` and start it again.
3. Ask the same question. The answer now follows your rule.

Then replace the sample with your own material: your rules and notes in
`knowledge/`, your job for the assistant in `AGENTS.md`, your checks in
`evals/`. Only what is in the files is shared. A correction in chat is not saved
on its own. [Authoring a hub](https://github.com/hubzoid/hubzoid/blob/main/docs/authoring-a-hub.md)

<details>
<summary>Prefer to set up with Claude Code or Codex?</summary>

Paste this into your coding assistant:

```text
Help me set up a Hubzoid agent using the quickstart:
https://github.com/hubzoid/hubzoid/blob/main/docs/quickstart.md

Use a new folder and a Python 3.11 or 3.12 virtual environment.
Help me choose an available runtime without pasting secrets into chat.
Run the starter and show me one answer from its house rules.
Then help me turn it into an agent for one task, using material I choose.
Show me each file change before you make it.
```

Your coding assistant helps you write the hub. Its conversation with you is not
saved into the hub.

</details>

## Use a ready-to-use hub

Complete agents built on Hubzoid, each in its own repo. Clone one and follow its
README.

| Hub | What it does | You need |
|---|---|---|
| [Software Factory](https://github.com/hubzoid/software-factory-hub) | Turns issues into reviewed draft pull requests, with people on the judgment calls | Docker, a GitHub repo, Claude Code or Codex |
| [Order & Invoice Desk](https://github.com/hubzoid/order-desk-hub) | Organizes orders and invoices from your inbox, every line tied to its source | An inbox, Claude Code or Codex. An ERP is optional |
| [Customer Desk](https://github.com/hubzoid/customer-desk-hub) | Drafts customer replies from your knowledge and systems. A person approves each one | A helpdesk, Claude Code or Codex. A labelled sample is included |
| [GitZoid](https://github.com/hubzoid/gitzoid-hub) | Pull request reviews and answers about your code | Claude Code or Codex. A GitHub token for your own repos |

```bash
git clone https://github.com/hubzoid/software-factory-hub software-factory
```

Each hub's README has a setup prompt for Claude Code or Codex, too.

## One hub, three ways to use it

| You want to | Use |
|---|---|
| Ask about your work and get a useful answer | **Chat**, with your hub's knowledge and permitted tools |
| Repeat a report or check, with saved inputs and results | **Workflows**, on demand or on a schedule. [Reports and sharing](https://github.com/hubzoid/hubzoid/blob/main/docs/reports-and-email.md) |
| Bring the same capabilities into your own assistant | **MCP**, with your own access. [Connect your assistant](https://github.com/hubzoid/hubzoid/blob/main/docs/mcp-server.md) |

For Claude Code, paste the connection command `hubzoid run` prints, then open
`/mcp` in Claude Code and approve the connection.

## Share with your team

Local mode is private to your machine. To share, turn on sign-in and set the
public address in `my-hub/.env`:

```dotenv
HUBZOID_AUTH=true
HUBZOID_PUBLIC_URL=https://hub.example.com
```

Then create the first administrator and start the hub:

```bash
hubzoid admin create you@example.com my-hub --owner
hubzoid run my-hub
```

Open the one-time sign-in link it prints, set a password, and add teammates in
**Settings → People & access**. Give each person the agents and capabilities they
need. Shared knowledge does not mean shared credentials or access to another
person's private results.

For a public deployment, use HTTPS and persistent storage.
[Team setup](https://github.com/hubzoid/hubzoid/blob/main/docs/ADMINISTRATION.md) ·
[Deployment](https://github.com/hubzoid/hubzoid/blob/main/docs/DEPLOYING.md) ·
[Backup and restore](https://github.com/hubzoid/hubzoid/blob/main/docs/BACKUP.md)

## Go further

| Next step | Guide |
|---|---|
| Add knowledge, skills, tools or specialist agents | [Authoring a hub](https://github.com/hubzoid/hubzoid/blob/main/docs/authoring-a-hub.md) |
| Connect business systems | [MCP connectors](https://github.com/hubzoid/hubzoid/blob/main/docs/mcp.md) |
| Write repeatable work | [Python workflows](https://github.com/hubzoid/hubzoid/blob/main/docs/workflows.md), [Markdown tasks](https://github.com/hubzoid/hubzoid/blob/main/docs/schedule.md) |
| Check answer quality and inspect runs | [Evals](https://github.com/hubzoid/hubzoid/blob/main/docs/evals.md), [observability](https://github.com/hubzoid/hubzoid/blob/main/docs/OBSERVABILITY.md) |
| Put the chat inside your own app | [Embedding the chat](https://github.com/hubzoid/hubzoid/blob/main/docs/embedding.md) |
| Use Slack or other channels | [Slack](https://github.com/hubzoid/hubzoid/blob/main/docs/slack.md), [other channels](https://github.com/hubzoid/hubzoid/blob/main/docs/inbound-surfaces.md) |
| Keep answering when a Claude login hits its limit | [Claude failover](https://github.com/hubzoid/hubzoid/blob/main/docs/DEPLOYING.md#claude-credential-failover) |
| Run on Windows, with uv, or from source | [Quickstart](https://github.com/hubzoid/hubzoid/blob/main/docs/quickstart.md) |

[All guides](https://github.com/hubzoid/hubzoid/blob/main/docs/README.md) ·
[Website documentation](https://hubzoid.com/docs) ·
[Release notes](https://github.com/hubzoid/hubzoid/blob/main/CHANGELOG.md) ·
[Upgrading](https://github.com/hubzoid/hubzoid/blob/main/docs/UPGRADING.md)

The chat and its Settings come with `pip install hubzoid`: no separate UI or
Node install. Hubzoid 2 no longer includes Open WebUI. Deployments that need it
stay on 1.x (`pip install "hubzoid<2"`). Python 3.12 workflows need SQLite 3.42
or later, or PostgreSQL. `hubzoid doctor my-hub` checks this.

Hubzoid is **Apache-2.0**, including team controls. Dependencies and brand assets
keep their own terms. [Licensing](https://github.com/hubzoid/hubzoid/blob/main/LICENSING.md).
To contribute, start with the [contributing guide](https://github.com/hubzoid/hubzoid/blob/main/CONTRIBUTING.md).
Need help with your company's agents? [Build with us](https://hubzoid.com/build-with-us)
uses the same open-source product.
