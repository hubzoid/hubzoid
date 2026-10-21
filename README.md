<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/hubzoid/hubzoid/72ac7bf1610c8a335b9b3ac3b536de8fdb6770f1/assets/mark-dark.svg">
    <img alt="Hubzoid" src="https://raw.githubusercontent.com/hubzoid/hubzoid/72ac7bf1610c8a335b9b3ac3b536de8fdb6770f1/assets/mark-light.svg" width="240">
  </picture>
</p>

<h3 align="center">One brain for your internal agents.</h3>
<p align="center">Your knowledge. Your way of working. Useful across your assistants and your team.</p>
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
workflows, or connect your own assistant through MCP.

Start with one useful job: a stock check, a policy answer, or a weekly report.
Keep the context and rules in files you can read, edit and version. Add teammates
with access to the agents and capabilities they need.

<p align="center">
  <img alt="Your agent folder becomes a shared Hub for chat, workflows and your assistant through MCP." src="https://raw.githubusercontent.com/hubzoid/hubzoid/72ac7bf1610c8a335b9b3ac3b536de8fdb6770f1/assets/shared-hub-light.svg" width="680">
</p>

## Get your first useful result

Use Claude Code or Codex with the setup prompt below, or run these commands
yourself.

Use **Python 3.11 or 3.12** in a virtual environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install hubzoid
hubzoid init my-hub
hubzoid run my-hub
```

The web app opens at **http://127.0.0.1:3080**. Ask:

> What should we reorder, and what could run out first?

The starter is an operations assistant for a **fictional home-goods shop**. It
uses a stock export and saved policies to explain what needs attention. You can
inspect the numbers and rules in `my-hub/`.

`init` detects a signed-in Claude Code or Codex CLI. Otherwise it offers to save
a model API key in the Hub's private `.env`. Chat needs model authentication;
provider charges or subscription limits apply. [Choose a runtime →](https://github.com/hubzoid/hubzoid/blob/main/docs/providers.md)

<details>
<summary>Prefer to set up with Claude Code or Codex?</summary>

Paste this into your coding assistant:

```text
Help me set up a local Hubzoid agent using the current quickstart:
https://github.com/hubzoid/hubzoid/blob/main/docs/quickstart.md

Use a new folder and a Python 3.11 or 3.12 virtual environment.
Help me choose an available runtime without pasting secrets into chat.
Run the operations sample and show me one result from its stock data.
Then help me adapt it to one task using material I choose.
Show me proposed file changes before replacing sample content.
```

Your coding assistant helps author the Hub. Its setup conversation is not
shared Hub knowledge. Authoring, runtime selection and MCP access are separate.

</details>

Local mode is private to your machine. Turn on sign-in before sharing access.
For Windows, uv, troubleshooting and source installation, see the
[quickstart](https://github.com/hubzoid/hubzoid/blob/main/docs/quickstart.md).

**Python 3.12 workflows need SQLite 3.42+ or PostgreSQL.** `hubzoid doctor my-hub`
checks the environment. This checkout is **2.0.1**; until it is published,
`pip install hubzoid` installs the earlier 1.x release. Use the
[source-install steps](https://github.com/hubzoid/hubzoid/blob/main/docs/quickstart.md#run-this-checkout)
to try this revision. Deployments that still need Open WebUI stay on 1.x
(`pip install "hubzoid<2"`).

## Make it work your way

Try a correction you can verify:

1. Ask the sample: **“A customer opened a blender 40 days ago and wants to
   return it. What can we offer?”** Its saved change-of-mind policy is 30 days.
2. Ask Claude Code or Codex to change the **change-of-mind** window to 45 days
   in `my-hub/knowledge/returns-policy.md`, keep the faulty-item rules unchanged,
   and show you the diff. Or edit the file yourself.
3. Restart the Hub and ask again. The answer should apply the saved 45-day
   window and the policy's other conditions.

Then replace the fictional policies and stock export with your own material.
Only explicit Hub content is shared; a correction in chat alone does not become
shared knowledge or automatic memory.
[Learn to author a Hub →](https://github.com/hubzoid/hubzoid/blob/main/docs/authoring-a-hub.md)

## One Hub, three ways to use it

| You want to… | Use… |
|---|---|
| Ask about your work and get a useful answer | **Chat**, with your Hub's knowledge and permitted tools |
| Repeat a report or check, with saved inputs and results | **Workflows**, on demand or on a schedule |
| Bring the same capabilities into your own assistant | **MCP**, with your own access |

In **1.3.1**, offered reports can run from chat and have personal schedules.
**My workspace → Schedules & reports** shows available reports, recent results
and your schedules. Edit inputs and timing, pause or stop. Schedules need the
Hub running; email notices are off by default.
[Reports, permissions and sharing →](https://github.com/hubzoid/hubzoid/blob/main/docs/reports-and-email.md)

For Claude Code, use the connection command printed by `hubzoid run`, then open
`/mcp` in Claude Code and approve the connection. Other clients have their own
setup. [Connect your assistant →](https://github.com/hubzoid/hubzoid/blob/main/docs/mcp-server.md)

## Pick a starting point

| Sample | What you can try | Create it |
|---|---|---|
| **Operations** | Stock decisions, supplier delays and customer returns from sample data and policies | `hubzoid init my-hub` |
| **Watchtower** | A personal service report, an explicit rule correction and repeat runs; also a model-assisted incident check | `hubzoid init my-watchtower --template watchtower` |
| **Guided tour** | Explore Hub tools, knowledge and skills | `hubzoid init my-tour --template demo` |
| **GitZoid** | Review fictional pull requests against saved rules, then connect your own repos | `hubzoid init my-gitzoid --template gitzoid` |

Each sample includes instructions. Watchtower's
[Service snapshot walkthrough](https://github.com/hubzoid/hubzoid/blob/main/hubzoid/templates/watchtower/README.md#run-your-own-report-from-chat-131)
uses a model-free report; chat still uses your configured runtime.
GitZoid starts in preview mode with fictional data; reviews still use your
configured model. Its [setup guide](https://github.com/hubzoid/hubzoid/blob/main/hubzoid/templates/gitzoid/README.md)
covers GitHub access, existing PRs, publishing and costs.
For a small starting folder, use `--template minimal`.

## Share with your team

Turn on sign-in and set the public address in `my-hub/.env`, then create your
first administrator:

```dotenv
HUBZOID_AUTH=true
HUBZOID_PUBLIC_URL=https://hub.example.com
```

```bash
hubzoid admin create you@example.com my-hub --owner
```

Open the one-time sign-in link, set a password, and add teammates through
**Settings → People & access**. Grant access to the agents and capabilities they
need. Shared knowledge does not mean shared credentials or access to another
person's private results.

For a public deployment, configure HTTPS and persistent storage, then restart.
[Team setup](https://github.com/hubzoid/hubzoid/blob/main/docs/ADMINISTRATION.md) ·
[Deployment](https://github.com/hubzoid/hubzoid/blob/main/docs/DEPLOYING.md) ·
[Backup and restore](https://github.com/hubzoid/hubzoid/blob/main/docs/BACKUP.md)

**Optional Claude failover (1.3.2 and later):** add up to two backup credentials to keep
answering when a Claude credential reaches a confirmed usage limit or its login
is rejected. Switching keeps the same model and permissions and never replays
work that has started. Existing single-credential setups are unchanged.
[Configuration, token replacement and limits →](docs/DEPLOYING.md#claude-credential-failover)

## The web app

`pip install hubzoid` includes the chat and its Settings, built with
assistant-ui and bundled with Hubzoid. No separate UI package or Node
installation is needed. Hubzoid 2.0 no longer offers Open WebUI; deployments
that still need it stay on 1.x. When upgrading an existing deployment, read the
[upgrade guide](https://github.com/hubzoid/hubzoid/blob/main/docs/UPGRADING.md)
first.

## Go further

| Next step | Guide |
|---|---|
| Add knowledge, skills, tools or specialist agents | [Hub authoring](https://github.com/hubzoid/hubzoid/blob/main/docs/authoring-a-hub.md) |
| Connect business systems | [MCP connectors](https://github.com/hubzoid/hubzoid/blob/main/docs/mcp.md) |
| Write repeatable work | [Python workflows](https://github.com/hubzoid/hubzoid/blob/main/docs/workflows.md), [Markdown tasks](https://github.com/hubzoid/hubzoid/blob/main/docs/schedule.md) |
| Check answer quality and inspect runs | [Evals](https://github.com/hubzoid/hubzoid/blob/main/docs/evals.md), [observability](https://github.com/hubzoid/hubzoid/blob/main/docs/OBSERVABILITY.md) |
| Use Slack or other channels | [Slack](https://github.com/hubzoid/hubzoid/blob/main/docs/slack.md), [other channels](https://github.com/hubzoid/hubzoid/blob/main/docs/inbound-surfaces.md) |

[All guides](https://github.com/hubzoid/hubzoid/blob/main/docs/README.md) ·
[Website documentation](https://hubzoid.com/docs) ·
[Release notes](https://github.com/hubzoid/hubzoid/blob/main/CHANGELOG.md)

Hubzoid is **Apache-2.0**, including team controls. Dependencies and brand assets
retain their own terms. [Licensing](https://github.com/hubzoid/hubzoid/blob/main/LICENSING.md).
To contribute, start with the [contributing guide](https://github.com/hubzoid/hubzoid/blob/main/CONTRIBUTING.md).
Need help with your company's agent? [Build with us](https://hubzoid.com/enterprise)
uses the same open-source product.
