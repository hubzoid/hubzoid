# Quickstart

Start with Python 3.11 or 3.12 in a virtual environment. Interactive setup detects
authenticated Claude or Codex CLIs and saves your choice. Without a selection,
`MODEL=claude-local` remains the default and needs a signed-in Claude CLI. For
Codex prerequisites or an API provider, see [providers.md](providers.md).
Open WebUI and the Python runtime adapters are installed with Hubzoid.

**Workflows on Python 3.12:** the workflow engine (DBOS, which runs markdown
schedules and code workflows) needs SQLite 3.42 or newer, or PostgreSQL.
Check with the same Python the hub uses:
`python -c "import sqlite3; print(sqlite3.sqlite_version)"`.
`hubzoid doctor` reports it as `deps.sqlite`. Python 3.11 is not affected.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install hubzoid
hubzoid init my-hub
hubzoid run my-hub
```

Open [localhost:3080](http://localhost:3080). Select the agent and say
“Say hello using the hello skill.” The default template is a minimal assistant,
with one worked example of a skill, knowledge file, custom tool and sub-agent.
For the interactive framework tour, use `hubzoid init guided-hub --template demo`.

The first local session uses Open WebUI's `admin@localhost` account. Hubzoid
provisions this verified owner once with Console administration and entry to the
hub. New hubs use managed permissions immediately. A later login never restores
revoked permissions. There is no second password or separate Console sign-in.
This local mode must not be exposed as a shared deployment.

## Make it useful

1. Edit `my-hub/AGENTS.md` to describe a real task and the expected result.
2. Add a small reference file under `knowledge/` and ask a question about it.
3. Add tools only when the agent needs to act. Put permission-sensitive tools
   under `restricted/` and grant their capabilities through the Console.
4. Run `hubzoid doctor my-hub`, restart after configuration changes, and try
   the same task again. Add an [eval](evals.md) for behaviour you rely on.

Open `/portal/` for Agents, People and Activity. Usage totals sit above the
agent cards; runs and schedules live inside each agent. New deployments
start with empty usage and run history. Send a chat or run a workflow before
expecting results there.

## First workflow

Start with a [Markdown task](schedule.md) for a plain-language recurring brief.
For explicit Python steps, the scaffold can run without a model or external API:

```bash
hubzoid new workflow first-check my-hub
hubzoid schedule run my-hub first_check
```

Open **Console → Agents → your agent → Runs & schedules** to inspect its result
and steps. The scaffold is manual. Scheduling is a separate, deliberate choice.
See [workflows.md](workflows.md) for scheduling, retries and recovery.

## Run this checkout

A development branch can be ahead of the package on PyPI. To exercise its code:

```bash
git clone https://github.com/hubzoid/hubzoid.git
cd hubzoid
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
hubzoid init my-hub
hubzoid run my-hub
```

Checkout-specific tests use this source installation. Installing from PyPI tests
the published version instead.

## Troubleshooting

- **No reply:** verify the CLI login or provider key and model in `.env`.
- **No agent available:** the chat shows why. A blocked account is told so; an
  account with no agent is told to ask an administrator, who grants **Use this
  agent** or an agent capability that implies entry. Organization-wide admin
  rights alone do not grant chat.
- **Console forbidden:** use the owner's account, or ask that owner for
  **Manage access**. A direct agent grant includes basic chat; organization-wide
  admin rights alone do not grant chat or restricted tools.
- **Connection unavailable:** verify the bridge process and logs. Restart after
  changing bridge credentials so the chat app receives current wiring.
- **Configuration issue:** `hubzoid doctor my-hub` reports loader and setup errors.

## Share with a team

The local session above has no sign-in. To give teammates their own accounts,
run the agents behind a gateway with sign-in on and an Open WebUI service
account:

```bash
export WEBUI_AUTH=true
export WEBUI_SECRET_KEY='a-long-random-value'
export HUBZOID_GATEWAY_ADMIN_EMAIL=you@example.com
export HUBZOID_GATEWAY_ADMIN_PASSWORD='a-strong-password'
hubzoid gateway ./my-hub --data-dir ./gateway-data
```

Sign in with that email and password, open the **Admin Console** from the chat
sidebar, and add teammates with **Add user** on an agent's Access tab (or on
People): choose an existing account, or create a new one with its access in one
step, then share the sign-in details yourself. With Google sign-in and
`OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true` configured, **Google sign-in only** creates
an account with no password to share ([auth.md](auth.md)). Public sign-up stays closed. On a gateway set up this way, Open WebUI's
own user list is hidden, so accounts are managed in one place: its Users section
opens on Groups, and Evaluations and Functions stay in the Admin Panel. Set
`HUBZOID_HIDE_OWUI_USERS=false` to keep Open WebUI's user list.

Before going live, read [administration](ADMINISTRATION.md),
[authentication](auth.md), and [deployment](DEPLOYING.md).
