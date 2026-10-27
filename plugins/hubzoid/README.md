# Hubzoid

Build and run internal AI agents with [Hubzoid](https://hubzoid.com), the
open-source platform for internal AI agents, from Claude Code or Codex. You describe a
task, point to your material and state your rules. The plugin's `build-hub`
skill turns them into a Hub, a folder of plain files, then tests it, saves a
correction you choose and runs it on your machine.

## Install

In Claude Code 2.1.275 or later:

```text
/plugin install hubzoid --marketplace hubzoid/hubzoid
```

Or from your shell:

```bash
claude plugin marketplace add hubzoid/hubzoid
claude plugin install hubzoid@hubzoid
```

Then ask "Set up a Hubzoid hub for my work", or run `/hubzoid:build-hub`.

In Codex:

```bash
codex plugin marketplace add hubzoid/hubzoid
codex plugin add hubzoid@hubzoid
```

Then ask Codex to set up a Hubzoid hub for your work.

## What it does

For a new Hub:

1. Asks for your task, an example of a good answer, the files to use, your rules,
   which actions need a person's approval and a usage limit.
2. Installs Hubzoid in a virtual environment and creates a Hub.
3. Turns your material into knowledge and rules files, keeping the originals
   unchanged in `raw_data/sources/`.
4. Sets approvals and usage limits, gets a first answer and runs `hubzoid doctor`.
5. Saves a correction you choose in a file and shows the next answer changing.
6. Adds evals, starts the Hub, restarts it and asks it again.
7. With your go-ahead, connects your assistant over MCP or gives a teammate access.

For an existing Hub, it makes the change you ask for, such as new knowledge, an
approval, an eval or a teammate's access, and runs the checks for that change.

It shows you the folder and a diff after each change, and ends with a hand-over:
what it built, every command it ran, and what is not done.

## What it runs, sends and stores

**Runs on your machine.** `pip install hubzoid` from PyPI into a `.venv` in your
folder, then the `hubzoid` command line. The Hub listens on 127.0.0.1 unless you
agree to open it to your network.

**Fetches.** Hubzoid's documentation as Markdown from
`raw.githubusercontent.com/hubzoid/hubzoid`, to read, never to run.

**Sends to the model you choose.** The material you pick, your rules, the
requests and the answers go to the model the Hub runs on: your Claude Code
sign-in (Anthropic), your Codex sign-in (OpenAI) or a provider whose key you
type yourself. Evals with criteria also send each case and answer to a judge
model, which is the Hub's own model unless you pick another.

**Stores locally.** Your files stay in the Hub folder. Chat and run history,
usage and eval results are kept in the Hub's `.hubzoid/` folder.

**Only if you set it up.** A connector you add (an MCP server) receives the
requests that use it, with the access you give it. An observability endpoint you
configure (OpenTelemetry or Langfuse) receives traces, and `hubzoid eval run`
then also sends eval results there. Teammates you grant access can use the Hub.

Neither the plugin nor Hubzoid sends anything to Hubzoid the company.

**Asks first.** It asks before turning on sign-in, creating accounts, granting
access, enabling a schedule or reaching outside your machine. It never writes
secrets to files or chat, and never approves a request or consent page for you.

## Requirements

- Python 3.11 or 3.12.
- Claude Code or Codex signed in on this machine, or a model API key.
- A macOS or Linux shell. The commands use `.venv/bin` paths.

## More

- [Quickstart](https://hubzoid.com/docs/getting-started/quickstart)
- [Source and issues](https://github.com/hubzoid/hubzoid)
- License: Apache-2.0
