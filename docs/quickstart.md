# Quickstart

Start with Python 3.11 or 3.12 in a virtual environment. With
[uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install hubzoid
hubzoid init my-hub
hubzoid run my-hub
```

Or with pip:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install hubzoid
hubzoid init my-hub
hubzoid run my-hub
```

On Windows PowerShell, use Python 3.12. Calling the virtual environment's
Python directly avoids activation-policy changes:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install hubzoid
.\.venv\Scripts\python -m hubzoid init my-hub
.\.venv\Scripts\python -m hubzoid run my-hub
```

`pip install hubzoid` installs the Hubzoid web app and the Python runtime
adapters. Open WebUI is not part of Hubzoid 2.0 (see [upgrading](UPGRADING.md#upgrading-to-20-open-webui-removed)).

**Workflows on Python 3.12:** the workflow engine (DBOS, which runs markdown
schedules and code workflows) needs SQLite 3.42 or newer, or PostgreSQL.
Check with the same Python the hub uses:
`python -c "import sqlite3; print(sqlite3.sqlite_version)"`.
`hubzoid doctor` reports it as `deps.sqlite`. Python 3.11 is not affected.

## What `init` and `run` do

`hubzoid init my-hub` creates a small starter and, in a fresh parent folder, an
agents-repo wrapper (`requirements.txt`, `.gitignore`, `README.md`):

```text
my-hub/
  AGENTS.md                  the assistant's job (Kestrel & Oak, a fictional shop)
  README.md                  what each folder is for
  .env                       the model
  knowledge/house-rules.md   three house rules to ask about and edit
  evals/refund-window.md     a check that the refund answer follows the rules
  connectors/.mcp.json       MCP connections, empty
  skills/  agents/  tools_local/  workflows/  schedule/  raw_data/   empty, ready for your files
```

Every folder Hubzoid reads is there, so you can see the whole layout.
[Authoring a hub](authoring-a-hub.md) explains each one.

For a complete agent instead, clone a ready-to-use hub: Software Factory,
Order & Invoice Desk, Customer Desk, or the GitZoid sample. `init` prints their
clone commands, and the [README](../README.md#use-a-ready-to-use-hub) lists
what each needs.

For the model, init uses a signed-in Claude Code or Codex CLI when it finds
one (it asks when it finds both). Otherwise, in a terminal, it offers to save
an OpenRouter, Anthropic or OpenAI API key in `my-hub/.env`, which is created
readable only by you. Without a terminal it asks nothing and the hub keeps
`MODEL=claude-local`. See [providers](providers.md).

`hubzoid run my-hub` starts the hub's bridge and the web app on one public
port (3080 by default, `--port` to change it) and prints one ready line:

```text
✓ Hubzoid is ready: http://127.0.0.1:3080  (local mode, sign-in off)
  Connect Claude Code: claude mcp add --transport http my-hub http://127.0.0.1:3080/mcp
```

From a terminal it opens the page in your browser (`--no-open` to skip). Pick
a suggested prompt such as "A customer wants a refund 40 days after delivery.
What can we offer?". The agent answers from `knowledge/house-rules.md` and
names it. To see a correction work, change the 30 days in that file, restart
`hubzoid run`, and ask again.

If Hubzoid gave your team a useful agent, a ⭐ on [GitHub](https://github.com/hubzoid/hubzoid) helps others find it.

## Local mode

Sign-in is off by default. You are the hub's owner, `admin@localhost`, an
administrator, and the public port listens on `127.0.0.1` only. `hubzoid run
--host 0.0.0.0` stops with an explanation until you turn sign-in on. Local mode
must not be exposed as a shared deployment.

Open **Settings** from the account menu for Agents, People & access and
Activity. Usage totals sit under **Usage overview** above the agents. Each agent holds
its access, connectors, runs and schedules, and evals. A new deployment starts with
empty usage and run history.

## Use an existing project

`init` is optional. A hub needs an `AGENTS.md`; knowledge, tools, skills and
workflows are optional. Write instructions for the **hub's runtime agent**
there. If the project already has an `AGENTS.md` for coding agents, put the
hub in a subfolder so the two prompts stay separate. Hubzoid reads under the
hub root, so choose that boundary deliberately.

```bash
cd existing-project
# Create AGENTS.md with the hub agent's purpose and instructions.
python -c 'from pathlib import Path; import secrets; p = Path(".env"); p.touch(mode=0o600, exist_ok=False); p.write_text("MODEL=claude-local\nBRIDGE_API_KEYS=" + secrets.token_urlsafe(32) + "\n")'
hubzoid doctor .
hubzoid run .
```

This creates a new `.env` and refuses to replace an existing one. If you
already have `.env`, add `MODEL` and a randomly generated `BRIDGE_API_KEYS`
yourself. Use a signed-in Claude CLI for `claude-local`, or choose another
[provider](providers.md). Plain Markdown in `AGENTS.md` works; optional
frontmatter supplies the display name, description and suggestions
([authoring](authoring-a-hub.md)). No application files need to be replaced.

To add a model-free workflow in the same project:

```bash
hubzoid new workflow first-check .
hubzoid schedule run . first_check
```

## Connect Claude Code

Hosted MCP is on for a local run. Paste the line `hubzoid run` printed:

```bash
claude mcp add --transport http my-hub http://127.0.0.1:3080/mcp
```

In Claude Code, run `/mcp`, choose the hub and authenticate. Your browser opens
Hubzoid's consent page. Choose **Allow connection** and return to Claude Code.
The hub's tools and knowledge are then available there under your access. See
[hosted MCP](mcp-server.md).

## Make it useful

With Claude Code or Codex, the [builder skill](../skills/builder/SKILL.md)
does these steps from your task, material and rules, with no clicks in Settings.

1. Edit `my-hub/AGENTS.md` to describe a real task and the expected result.
   Its `suggestions:` become the buttons on the empty chat screen.
2. Replace the files in `knowledge/` with your own and ask about them.
3. Add tools only when the agent needs to act. Put permission-sensitive tools
   under `restricted/` and grant their capabilities in Settings.
4. Save a correction explicitly in the relevant knowledge or instruction file,
   using Claude Code, Codex or your editor. A chat correction alone is not saved
   shared knowledge. Run `hubzoid doctor my-hub`, restart after configuration
   changes, and repeat the task to see the correction applied. Add an
   [eval](evals.md) for behaviour you rely on.
5. Put your logo in `branding/` ([branding](branding.md)).

## First workflow

Start with a [Markdown task](schedule.md) for a plain-language recurring brief.
For explicit Python steps, the scaffold can run without a model or external API:

```bash
hubzoid new workflow first-check my-hub
hubzoid schedule run my-hub first_check
```

Open **Settings → Agents → your agent → Runs & schedules** to inspect its result
and steps. The scaffold is manual. Scheduling is a separate, deliberate choice.
See [workflows.md](workflows.md) for scheduling, retries and recovery.

A workflow marked as a tool can also run from chat, and people can keep it
on their own schedule in **My workspace → Schedules & reports** once they have
the grants ([reports and sharing](reports-and-email.md)). The hub must be
running when a schedule is due.

## Share with a team

Turn sign-in on and create the first administrator:

```bash
# in my-hub/.env
HUBZOID_AUTH=true
HUBZOID_PUBLIC_URL=https://hub.example.com   # the address people open

hubzoid admin create you@example.com my-hub --owner
hubzoid run my-hub
```

`hubzoid admin create` prints a one-time sign-in link. Open it, set a password,
and you are signed in as an Administrator with access to the hub. Add
teammates under **People & access → Add user** in Settings: choose their
access, then share the one-time sign-in link it gives you. Nothing is emailed.
From the command line, `hubzoid admin create teammate@example.com my-hub`
prints the link and `hubzoid grant teammate@example.com use_hub my-hub` gives
them the agent.
With Google, Microsoft or an OpenID Connect provider configured, people can
sign in with it instead ([authentication](auth.md)).

For several agents behind one address, put a `hubzoid.yaml` in the folder that
holds the hub folders, and run `hubzoid run` there:

```yaml
version: 1
sign_in: true
owner: you@example.com
hubs:
  - sales
  - support
```

```bash
export HUBZOID_ADMIN_PASSWORD='a-strong-bootstrap-password'   # first start only
hubzoid run
```

Each hub runs in its own process. One sign-in covers every agent a person may
use. [Deployment](DEPLOYING.md#several-hubs-on-one-address-hubzoidyaml) has the
full file.

Before going live, read [administration](ADMINISTRATION.md),
[authentication](auth.md) and [deployment](DEPLOYING.md): a public deployment
needs a TLS proxy and `HUBZOID_PUBLIC_URL`.

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

The web app, Settings included, is served from the built bundle in
`hubzoid/portal_dist`. After changing anything in `portal/`, rebuild it with
`cd portal && npm ci && npm run build`. Checkout-specific tests use this source
installation. Installing from PyPI tests the published version instead.

## Troubleshooting

- **No reply:** check the CLI login, or the provider key and `MODEL`, in `.env`.
- **"You don't have access to an agent yet":** with sign-in on, an
  administrator grants **Use this agent** (or a capability that includes it)
  in Settings. Organization-wide admin rights alone do not grant chat.
- **Settings says you can't manage access:** use an Administrator account,
  or ask one for **Manage access** on the agent.
- **`hubzoid run` refuses `--host 0.0.0.0`:** that is local mode protecting
  you. Turn sign-in on first ([authentication](auth.md)).
- **The port is busy:** each running hub needs a unique public port **and**
  bridge port: `hubzoid run my-hub --port 3090 --bridge-port 8001`.
  `--port` alone does not change the bridge's default port, 8000.
- **Configuration issue:** `hubzoid doctor my-hub` reports loader and setup errors.
