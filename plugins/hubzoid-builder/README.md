# Hubzoid Builder

Hubzoid Builder helps Claude Code build an internal AI agent with
[Hubzoid](https://hubzoid.com), the open-source platform for internal AI agents.
You describe a task, point it at your material and state your rules. It builds a
Hub, a folder of plain files, then tests it, saves a correction you choose and
runs it on your machine.

## Install

In Claude Code:

```text
/plugin install hubzoid-builder --marketplace hubzoid/hubzoid
```

Or from your shell:

```bash
claude plugin marketplace add hubzoid/hubzoid
claude plugin install hubzoid-builder@hubzoid
```

Then ask: "Set up a Hubzoid hub for my work." The `hubzoid-builder` skill takes
over.

## What it does

1. Asks for your task, an example of a good answer, the files to use, your rules,
   which actions need a person's approval and a spending limit.
2. Installs Hubzoid in a virtual environment and creates a Hub.
3. Turns your material into knowledge and rules files, keeping the originals
   unchanged in `raw_data/sources/`.
4. Sets approvals and usage limits, gets a first answer and runs `hubzoid doctor`.
5. Saves a correction you choose in a file and shows the next answer changing.
6. Adds evals, starts the Hub, restarts it and asks it again.
7. With your go-ahead, connects your assistant over MCP or gives a teammate access.

It shows you the folder and a diff after each change, and ends with a hand-over:
what it built, every command it ran, and what is not done.

## What it runs and fetches

- `pip install hubzoid` from PyPI into a `.venv` in your folder.
- The `hubzoid` command line, on your machine. The Hub listens on 127.0.0.1
  unless you agree otherwise.
- Hubzoid's documentation from `raw.githubusercontent.com/hubzoid/hubzoid`.
- Your answers come from the model you choose: your Claude Code or Codex sign-in,
  or a provider key you type yourself. The plugin sends nothing anywhere else.

It asks first before turning on sign-in, creating accounts, granting access,
enabling a schedule or reaching outside your machine. It never writes secrets to
files or chat, and never approves a request or consent page for you.

## Requirements

Python 3.11 or 3.12, and Claude Code or Codex signed in, or a model API key.

## More

- [Quickstart](https://hubzoid.com/docs/getting-started/quickstart)
- [Source and issues](https://github.com/hubzoid/hubzoid)
- License: Apache-2.0
