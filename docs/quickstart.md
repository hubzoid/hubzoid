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

`pip install hubzoid` installs the Hubzoid web app and the Python runtime
adapters. It no longer installs Open WebUI (see [upgrading](UPGRADING.md)).

**Workflows on Python 3.12:** the workflow engine (DBOS, which runs markdown
schedules and code workflows) needs SQLite 3.42 or newer, or PostgreSQL.
Check with the same Python the hub uses:
`python -c "import sqlite3; print(sqlite3.sqlite_version)"`.
`hubzoid doctor` reports it as `deps.sqlite`. Python 3.11 is not affected.

## What `init` and `run` do

`hubzoid init my-hub` creates the hub folder and, in a fresh parent folder, an
agents-repo wrapper (`requirements.txt`, `.gitignore`, `README.md`). The
default template is an operations assistant for Kestrel & Oak, a fictional
home-goods shop: five policy files, a stock export, a `stock_check` tool and
three suggested prompts. Other templates: `--template minimal` (one example of
each file type), `--template demo` (a guided tour) and `--template watchtower`
(a workflow-first sample).

For the model, init uses a signed-in Claude Code or Codex CLI when it finds
one (it asks when it finds both). Otherwise, in a terminal, it offers to save
an OpenRouter, Anthropic or OpenAI API key in `my-hub/.env`, which is created
readable only by you. Without a terminal it asks nothing and the hub keeps
`MODEL=claude-local`. See [providers](providers.md).

`hubzoid run my-hub` starts the hub's bridge and the web app on one public
port (3080 by default, `--port` to change it) and prints one ready line:

```text
✓ Hubzoid is ready: http://127.0.0.1:3080  (local mode, sign-in off)
  Connect Claude Code: claude mcp add --transport http kestrel-oak-ops http://127.0.0.1:3080/mcp
```

From a terminal it opens the page in your browser (`--no-open` to skip). Pick
a suggested prompt such as "What should we reorder today, and what could run
out first?". The agent answers from the hub's files and its `stock_check`
tool, and you see each tool step as it runs.

If Hubzoid gave your team a useful agent, a ⭐ on [GitHub](https://github.com/hubzoid/hubzoid) helps others find it.

## Local mode

Sign-in is off by default. You are the hub's owner, `admin@localhost`, an
administrator, and the public port listens on `127.0.0.1` only. `hubzoid run
--host 0.0.0.0` stops with an explanation until you turn sign-in on. Local mode
must not be exposed as a shared deployment.

Open **Admin Console** from the account menu (or `/portal/`) for Agents,
People and Activity. Usage totals sit above the agent cards. Each agent holds
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
([authoring](authoring.md)). No application files need to be replaced.

To add a model-free workflow in the same project:

```bash
hubzoid new workflow first-check .
hubzoid schedule run . first_check
```

## Connect Claude Code

Hosted MCP is on for a local run. Paste the line `hubzoid run` printed:

```bash
claude mcp add --transport http kestrel-oak-ops http://127.0.0.1:3080/mcp
```

In Claude Code, run `/mcp`, choose the hub and authenticate. Your browser opens
Hubzoid's consent page. Choose **Allow connection** and return to Claude Code.
The hub's tools and knowledge are then available there under your access. See
[hosted MCP](mcp-server.md).

## Make it useful

1. Edit `my-hub/AGENTS.md` to describe a real task and the expected result.
   Its `suggestions:` become the buttons on the empty chat screen.
2. Replace the files in `knowledge/` with your own and ask about them.
3. Add tools only when the agent needs to act. Put permission-sensitive tools
   under `restricted/` and grant their capabilities in the Console.
4. Run `hubzoid doctor my-hub`, restart after configuration changes, and try
   the same task again. Add an [eval](evals.md) for behaviour you rely on.
5. Put your logo in `branding/` ([branding](branding.md)).

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
teammates under **People → Add user** in the Admin Console: choose their
access, then share the one-time sign-in link it gives you. Nothing is emailed.
With Google, Microsoft or an OpenID Connect provider configured, people can
sign in with it instead ([authentication](auth.md)).

For several agents behind one address, give each hub a unique `BRIDGE_PORT`
in its `.env` (for example 8000 and 8001), then run a gateway with sign-in set
once in the gateway's environment:

```bash
export HUBZOID_AUTH=true
export HUBZOID_ADMIN_EMAIL=you@example.com
export HUBZOID_ADMIN_PASSWORD='a-strong-password'
hubzoid gateway ./sales ./support --data-dir ./gateway-data
```

On the first start the gateway creates that administrator. Sign in with that
email and password. One sign-in covers every agent a person may use.

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

The web app and the Console are served from the built bundle in
`hubzoid/portal_dist`. After changing anything in `portal/`, rebuild it with
`cd portal && npm ci && npm run build`. Checkout-specific tests use this source
installation. Installing from PyPI tests the published version instead.

## Troubleshooting

- **No reply:** check the CLI login, or the provider key and `MODEL`, in `.env`.
- **"You don't have access to an agent yet":** with sign-in on, an
  administrator grants **Use this agent** (or a capability that includes it)
  in the Console. Organization-wide admin rights alone do not grant chat.
- **The Console says you can't manage access:** use an Administrator account,
  or ask one for **Manage access** on the agent.
- **`hubzoid run` refuses `--host 0.0.0.0`:** that is local mode protecting
  you. Turn sign-in on first ([authentication](auth.md)).
- **The port is busy:** each running hub needs a unique public port **and**
  bridge port: `hubzoid run my-hub --port 3090 --bridge-port 8001`.
  `--port` alone does not change the bridge's default port, 8000.
- **Configuration issue:** `hubzoid doctor my-hub` reports loader and setup errors.
