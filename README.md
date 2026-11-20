<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/hubzoid/hubzoid/72ac7bf1610c8a335b9b3ac3b536de8fdb6770f1/assets/mark-dark.svg">
    <img alt="Hubzoid" src="https://raw.githubusercontent.com/hubzoid/hubzoid/72ac7bf1610c8a335b9b3ac3b536de8fdb6770f1/assets/mark-light.svg" width="240">
  </picture>
</p>

<h3 align="center">One brain for your internal agents.</h3>
<p align="center">Provide context once. Reuse it across workflows, chat, and the AI tools your team already uses.</p>
<p align="center">
  <a href="https://youtu.be/La2hApf4vQc">Watch the video</a> ·
  <a href="https://github.com/hubzoid/hubzoid#quick-start">Quick start</a> ·
  <a href="https://hubzoid.com/docs">Documentation</a> ·
  <a href="https://github.com/hubzoid/hubzoid#ready-to-use-hubs">Ready-to-use hubs</a> ·
  <a href="https://hubzoid.com">Website</a> ·
  <a href="https://github.com/hubzoid/hubzoid/discussions">Discussions</a>
</p>
<p align="center">
  <a href="https://pypi.org/project/hubzoid/"><img src="https://img.shields.io/pypi/v/hubzoid?color=B5471F" alt="PyPI version"></a>
  <a href="https://github.com/hubzoid/hubzoid/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-0B0B0C" alt="Apache License 2.0"></a>
  <a href="https://github.com/hubzoid/hubzoid/discussions"><img src="https://img.shields.io/badge/Community-Discussions-0B0B0C?logo=github" alt="GitHub Discussions"></a>
</p>

**Hubzoid turns a folder of your knowledge, rules and tools into an agent you can use yourself and share with your team.** Ask it questions in chat, run repeatable workflows, or connect the AI assistant you already use. The rules live in files you can read, edit and version.

<p align="center">
  <a href="https://youtu.be/La2hApf4vQc"><img alt="Watch: Hubzoid, one brain for your internal agents" src="https://img.youtube.com/vi/La2hApf4vQc/maxresdefault.jpg" width="640"></a>
</p>

## Why Hubzoid

- **Your rules live in files you own.** Knowledge, skills and agent instructions are Markdown in a folder. Read them, edit them, version them in Git. [Authoring a hub](https://github.com/hubzoid/hubzoid/blob/main/docs/authoring-a-hub.md)
- **One hub, three ways to use it.** Ask in **chat**, run repeatable **workflows** on demand or on a schedule, or bring the same knowledge and tools into Claude Code, ChatGPT or Codex over **MCP**. [Connect your assistant](https://github.com/hubzoid/hubzoid/blob/main/docs/mcp-server.md)
- **Runs on the login you already have.** Claude Code or Codex signed in on your machine is enough. A model API key works too. [Choose a runtime](https://github.com/hubzoid/hubzoid/blob/main/docs/providers.md)
- **Built for a team.** Sign-in, access per person and per agent, approvals before consequential actions, and a record of every run. [Team setup](https://github.com/hubzoid/hubzoid/blob/main/docs/ADMINISTRATION.md)
- **Open source and yours to host.** Apache-2.0, including team controls. Run it on your laptop, then on a server you control. [Deploying](https://github.com/hubzoid/hubzoid/blob/main/docs/deploy/README.md)

## Quick start

You need Python 3.11 or 3.12, and Claude Code or Codex signed in on this machine. No API key.

Paste this into Claude Code or Codex:

```text
Set up Hubzoid for me using the quickstart:
https://github.com/hubzoid/hubzoid/blob/main/docs/quickstart.md
Run the starter, show me one answer, then help me make it mine.
```

The quickstart tells your assistant to use a virtual environment, keep secrets out of chat, and show you each file change before making it.

Or by hand:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install hubzoid
hubzoid init my-hub
hubzoid run my-hub
```

The web app opens at **http://127.0.0.1:3080**. Ask:

> A customer wants a refund 40 days after delivery. What can we offer?

The assistant answers from `knowledge/house-rules.md` and names the file. That is the whole loop: a rule in a file, an answer that follows it, and a source you can check.

<!-- Screenshot: the chat answer to the refund question, with house-rules.md named as the source. -->

`my-hub` is a small starter for Kestrel & Oak, a fictional shop. `init` uses the Claude Code or Codex you are signed in to. Without one, it offers to save a model API key in the hub's private `.env`. [Windows, uv or from source](https://github.com/hubzoid/hubzoid/blob/main/docs/quickstart.md)

## Make it yours

The starter is a folder. Every folder Hubzoid reads is there, with sample content only where it teaches the loop.

```text
my-hub/
├── AGENTS.md          the assistant's job: who it is, what it answers from
├── knowledge/         what it knows: rules, policies, notes, one Markdown file each
│   └── house-rules.md
├── skills/            step-by-step procedures it follows when a task matches
├── agents/            specialist agents it can hand work to
├── tools_local/       your own Python tools
├── connectors/        MCP connections to other systems
├── workflows/         repeatable work in Python
├── schedule/          tasks that run on a schedule, one Markdown file each
├── evals/             checks that answers stay right
│   └── refund-window.md
└── raw_data/          files the assistant can search and read
```

Change one rule and watch the answer follow it:

1. Open `my-hub/knowledge/house-rules.md` and change the refund window from 30 days to 45. Or ask Claude Code or Codex to make the change and show you the diff.
2. Stop `hubzoid run` and start it again.
3. Ask the same question. The answer now follows your rule. `hubzoid eval run my-hub` checks it stays that way.

Then replace the sample with your own material: your rules and notes in `knowledge/`, the assistant's job in `AGENTS.md`, your checks in `evals/`. Only what is in the files is shared. A correction in chat is not saved on its own, and your coding assistant's conversation with you is not saved into the hub. [Authoring a hub](https://github.com/hubzoid/hubzoid/blob/main/docs/authoring-a-hub.md)

## Ready-to-use hubs

Complete agents built on Hubzoid, each in its own repository. Each one starts with a labelled sample you can run before connecting anything of your own, and its README says what it needs.

| Hub | What it does |
|---|---|
| [Software Factory](https://github.com/hubzoid/software-factory-hub) | Turns engineering requests into reviewed draft pull requests. People approve the plan and merge. |
| [Order & Invoice Desk](https://github.com/hubzoid/order-desk-hub) | Reads orders and invoices from your inbox into sourced records and reports. No ERP required. |
| [Customer Desk](https://github.com/hubzoid/customer-desk-hub) | Drafts customer replies from your knowledge and systems. A person approves each one. |
| [Engineering Desk](https://github.com/hubzoid/engineering-desk-hub) | Reviews pull requests against your team's rules and answers questions about your code. Nothing is posted until you say so. |

## Use it three ways

| You want to | Use |
|---|---|
| Ask about your work and get an answer with its source | **Chat**, in the web app, in Slack or inside your own app, with the hub's knowledge and permitted tools. [Slack](https://github.com/hubzoid/hubzoid/blob/main/docs/slack.md) · [Other channels](https://github.com/hubzoid/hubzoid/blob/main/docs/inbound-surfaces.md) · [Embedding the chat](https://github.com/hubzoid/hubzoid/blob/main/docs/embedding.md) |
| Repeat a report or a check, with saved inputs and results | **Workflows**, on demand or on a schedule. [Workflows](https://github.com/hubzoid/hubzoid/blob/main/docs/workflows.md) · [Reports and sharing](https://github.com/hubzoid/hubzoid/blob/main/docs/reports-and-email.md) |
| Bring the same capabilities into your own assistant | **MCP**, with your own access. [Connect your assistant](https://github.com/hubzoid/hubzoid/blob/main/docs/mcp-server.md) |

<p align="center">
  <img alt="Your agent folder becomes a shared Hub for chat, workflows and your assistant through MCP." src="https://raw.githubusercontent.com/hubzoid/hubzoid/72ac7bf1610c8a335b9b3ac3b536de8fdb6770f1/assets/shared-hub-light.svg" width="680">
</p>

For Claude Code, paste the connection command `hubzoid run` prints, open `/mcp` in Claude Code and approve the connection.

## Host it for your team

Local mode is private to your machine. To share a hub, turn on sign-in and set its public address in the hub's `.env`:

```dotenv
HUBZOID_AUTH=true
HUBZOID_PUBLIC_URL=https://hub.example.com
```

Create the first administrator, then start the hub:

```bash
hubzoid admin create you@example.com my-hub --owner
hubzoid run my-hub
```

Open the one-time sign-in link it prints, set a password, and add teammates in **Settings → People & access**. Each person gets the agents and capabilities they need. Shared knowledge does not mean shared credentials or access to another person's private results.

Every way of hosting works the same way: one image built from the hub's Git repository, one durable folder at `/data`, sign-in on, one public address, and weekly verified backups.

| Where | How | Status |
|---|---|---|
| Docker or Compose on your own server | The released image and `docker/docker-compose.yml`. [Deploying](https://github.com/hubzoid/hubzoid/blob/main/docs/DEPLOYING.md) | Supported |
| A VM with systemd | A virtual environment behind a reverse proxy. [Deploying](https://github.com/hubzoid/hubzoid/blob/main/docs/DEPLOYING.md) | Supported |
| [Railway](https://github.com/hubzoid/hubzoid/blob/main/docs/deploy/railway.md) | `hubzoid deploy init railway`, then Railway builds the hub's `Dockerfile` with a volume at `/data`. | Written, not yet verified on Railway |
| [Maritime](https://github.com/hubzoid/hubzoid/blob/main/docs/deploy/maritime.md) | `hubzoid deploy init maritime`, then `maritime create --repo` with its `/data` disk. | Written, not yet verified on Maritime |

`hubzoid deploy verify https://your-address` checks a deployment from outside. [Deployment overview](https://github.com/hubzoid/hubzoid/blob/main/docs/deploy/README.md) · [Backup and restore](https://github.com/hubzoid/hubzoid/blob/main/docs/BACKUP.md) · [Upgrading](https://github.com/hubzoid/hubzoid/blob/main/docs/UPGRADING.md)

## Go further

| Next step | Guide |
|---|---|
| Add knowledge, skills, tools or specialist agents | [Authoring a hub](https://github.com/hubzoid/hubzoid/blob/main/docs/authoring-a-hub.md) |
| Connect business systems | [MCP connectors](https://github.com/hubzoid/hubzoid/blob/main/docs/mcp.md) |
| Write repeatable work | [Python workflows](https://github.com/hubzoid/hubzoid/blob/main/docs/workflows.md), [Markdown tasks](https://github.com/hubzoid/hubzoid/blob/main/docs/schedule.md) |
| Ask a person before an action runs | [Approvals and hub changes](https://github.com/hubzoid/hubzoid/blob/main/docs/approvals-and-hub-changes.md), [Work items](https://github.com/hubzoid/hubzoid/blob/main/docs/work-items.md) |
| Check answer quality and inspect runs | [Evals](https://github.com/hubzoid/hubzoid/blob/main/docs/evals.md), [Observability](https://github.com/hubzoid/hubzoid/blob/main/docs/OBSERVABILITY.md) |
| Control access and spending | [Access management](https://github.com/hubzoid/hubzoid/blob/main/docs/access-management.md), [Budgets](https://github.com/hubzoid/hubzoid/blob/main/docs/budgets.md) |
| Keep answering when a Claude login hits its limit | [Claude failover](https://github.com/hubzoid/hubzoid/blob/main/docs/DEPLOYING.md#claude-credential-failover) |

[All guides](https://github.com/hubzoid/hubzoid/blob/main/docs/README.md) · [Website documentation](https://hubzoid.com/docs) · [Release notes](https://github.com/hubzoid/hubzoid/blob/main/CHANGELOG.md)

The chat and its Settings come with `pip install hubzoid`: no separate UI or Node install. Hubzoid 2 no longer includes Open WebUI. Deployments that need it stay on 1.x (`pip install "hubzoid<2"`). Python 3.12 workflows need SQLite 3.42 or later, or PostgreSQL. `hubzoid doctor my-hub` checks this.

## Help, contributing and license

Questions and ideas go to [GitHub Discussions](https://github.com/hubzoid/hubzoid/discussions). Bugs go to [issues](https://github.com/hubzoid/hubzoid/issues). To contribute, start with the [contributing guide](https://github.com/hubzoid/hubzoid/blob/main/CONTRIBUTING.md).

Hubzoid is **Apache-2.0**, including team controls. Dependencies and brand assets keep their own terms. [Licensing](https://github.com/hubzoid/hubzoid/blob/main/LICENSING.md)

Need help with your company's agents? [Build with us](https://hubzoid.com/build-with-us) uses the same open-source product.
