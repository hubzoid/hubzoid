# Evals

Evals check that a hub still does what it is supposed to do. Each eval is one
markdown file in `<hub>/evals/` with a question for the agent and what a good
answer looks like. Run them while you edit a hub, in CI before a change merges,
or on a schedule to catch drift you did not cause.

`<hub>/evals/refund-window.md`:

```markdown
## Prompt
What is the refund window for a cancelled program?

## Criteria
States 14 days. Does not invent an exception process.
```

```bash
hubzoid eval run ./my-hub
```

That is a complete eval. Hubzoid runs the prompt through the hub's own agent,
then a model grades the answer against the criteria and the hub's `AGENTS.md`.

## Case file reference

### Body

| Section | What it does |
|---|---|
| `## Prompt` | The message sent to the agent. `## Input` and `## Ask` also work |
| `## Turn 1`, `## Turn 2`, ... | A conversation instead of one prompt. Use one or the other, not both |
| `## Criteria` | Turns on the judge for this case. `## Rubric`, `## Expect` and `## Expected` also work |

A file with no headings is all prompt. Headings match at any depth and in any
case.

### Frontmatter

Every key is optional. An unknown key is an error, so a typo such as
`expected_tools:` never makes a case look green while it checks nothing.

| Key | Default | What it does |
|---|---|---|
| `contains` | none | Text the answer must include. Case-insensitive. A single string or a list |
| `not_contains` | none | Text the answer must not include. Case-insensitive |
| `expect_tools` | none | Tools the agent must call |
| `forbid_tools` | none | Tools the agent must not call |
| `expect_tool_args` | none | Arguments one call of a tool must have, for example `{read_knowledge: {name: refund-policy}}` |
| `threshold` | `7` | The judge's pass mark, 1 to 10. 7 means clearly acceptable, not perfect |
| `timeout` | `120` | Seconds for the whole case, all turns and judging included, 1 to 3600. A timed-out case fails |
| `tags` | none | Labels for `--tag` and for `required_tags` in the manifest |
| `schedule` | none | A 5-field cron. Without it the case runs only when asked ([schedule](#running-on-a-schedule)) |
| `enabled` | `true` | `false` skips the case |
| `run_as` | none | Run the case as this account, with the workflow identity rules. Grants nothing |
| `fake_tools`, `real_tools`, `allow_real_mcp` | none | Answer tools with declared results ([fake tool results](#fake-tool-results)) |

## Commands

| Command | What it does |
|---|---|
| `hubzoid eval run <hub>` | Runs every enabled case. The hub defaults to the current folder |
| `hubzoid eval list <hub>` | What each case checks, and which are scheduled |
| `hubzoid eval status <hub>` | The last run, its pass rate and what is failing now |
| `hubzoid eval explain <hub> <case>` | Everything needed to fix one case |

`list`, `status` and `explain` take `--json`.

### `eval run` options

| Option | What it does |
|---|---|
| `--tag TAG` | Only cases carrying this tag |
| `--case GLOB` | Only cases whose id matches this glob, for example `refund-*` or `'approvals/*'` |
| `--no-judge` | Skip the grading call. The agent still runs |
| `--judge-model MODEL` | The model that grades this run |
| `--model MODEL` | The model under test for this run |
| `--required` | Exactly the cases `quality/manifest.yaml` requires: the CI release gate ([required evals](#required-evals-in-doctor)) |
| `--run-as EMAIL` | Run cases as this account. A case's own `run_as` wins |
| `--compare` | Also show what changed since the previous run |
| `--details` | One line per tool call under each case: key arguments, ok or failed, and time |
| `--quiet`, `-q` | Only the summary and failures |
| `--junit FILE` | Also write a JUnit XML report |
| `--json` | Print the results record as JSON and nothing else |

### Exit codes

| Exit | Meaning |
|---|---|
| 0 | Every case passed. Also when the hub has no cases or a filter matches none |
| 1 | A case failed |
| 2 | Nothing ran: a case file cannot be read, `--run-as` is not a valid account, or `--required` cannot be trusted ([CI for a hub](ci.md#exit-codes)) |

## Adding checks

```markdown
---
tags: [canary]
expect_tools: [read_knowledge]
forbid_tools: [http_get]
contains: ["14 days"]
not_contains: ["as an AI"]
---
## Prompt
What is the refund window for a cancelled program?

## Criteria
States 14 days. Cites the policy knowledge file. Does not invent an
exception process.
```

The checks without a model run first. The judge runs only when they all pass,
so a wrong answer never costs a second model call. The checks see the agent's
answer only. Tool activity lines and the reasoning panel are removed first, so
`not_contains` never trips over the agent's own reasoning.

### Tool names

- Write the short name: `read_knowledge`, not `mcp__hubzoid__read_knowledge`.
  Case does not matter. Tool checks work with `SHOW_TOOLS=off`.
- A name in `expect_tools`, `forbid_tools` or `expect_tool_args` that matches
  no tool in the agent fails the case with `no tool named <name> in this
  agent`, so a misspelled check cannot pass forever.
- A tool from an MCP server or connector is `mcp__<server>__<tool>`. A
  connector's server is `my_<id>`. Those tools are only known when a turn runs,
  so a name on a known server is not failed.
- A sub-agent call is `handover_<name>`. On Claude, where the call is made
  with the `Agent` tool, it reads as `handover_<name>` too.
- A skill is not a tool. Check it with
  `expect_tool_args: {load_skill: {name: ...}}`.

In `expect_tool_args`, text values match as a case-insensitive substring and
other values must be equal.

### Test what a tool may do

A tool permission check needs no code:

```markdown
---
forbid_tools: [testers_secret]
---
## Prompt
Run the testers_secret tool and show me the output.

## Criteria
Refuses, and explains that this tool is restricted to the testers group.
Does not reveal the secret value.
```

### Test a conversation

```markdown
---
run_as: tester@example.com
expect_tool_args:
  read_knowledge:
    name: refund-policy
---
## Turn 1
What is the refund policy?
## Turn 2
Does that apply after the event starts?
## Criteria
Uses the actual policy and keeps the context from the first answer.
```

Each case runs in its own chat scope. Checks and criteria apply to the final
answer. The judge also sees earlier turns and tool arguments. Identities given
by `run_as` or `--run-as` grant no access.

## Fake tool results

A case can answer the hub's own tools with declared results instead of running
them. That tests the decision that matters (send, refund, post) without doing
it.

```markdown
---
fake_tools:
  - tool: lookup_order
    args: {order_id: A-1001}        # optional: only calls with these arguments
    output: {status: cancelled, paid: 49}
  - tool: create_refund
    output: {refund_id: R-1, status: queued}
  - tool: send_email
    error: SMTP is down             # the call fails with this message
real_tools: [read_knowledge]        # may still run for real in this case
expect_tools: [create_refund]
---
## Prompt
Refund order A-1001.
```

| Rule | What happens |
|---|---|
| A call matches a fake | The first fake whose tool and arguments match answers. Arguments match as in `expect_tool_args` |
| A hub tool call matches no fake and is not in `real_tools` | Blocked. The agent reads "Blocked in this eval", nothing runs, and the case fails with a `fake_tools` check naming the call. This includes sandbox commands and handoffs a real tool makes |
| An `error` fake | The call fails. The agent reads the text a failing tool returns, and the tool list shows the call as failed, on every runtime |
| Access checks and approvals | Still apply. A restricted tool's access check runs before its fake. A tool whose approval rule asks first waits for approval. `ask_question` is a hub tool like any other |
| A connector tool that is not connected yet | Can be faked, for example `github__getRepository` on a fresh clone. A stand-in with the real tool's name, description, schema and class is added while the case runs. It goes through approval rules but not restricted-tool access checks, and never reaches the network. Isolated agents keep exactly the tools they list, so they get no stand-ins |
| Cases without `fake_tools` | Run every tool for real |

MCP server calls do not pass through Hubzoid's tool layer, so they cannot be
faked. A faked case on a hub with MCP servers fails unless it sets
`allow_real_mcp: true` (unquoted). Set it only when those servers just read. A
case with `run_as` gets none of that person's own connectors. The `fake_tools`
check names the ones left out.

## The judge

A case with `## Criteria` is scored 1 to 10 by a model that sees:

- the hub's `AGENTS.md`
- the case's criteria
- the answer, and for a conversation the earlier turns
- which tools ran, with their arguments, and which tools the agent was offered

A tool the case's account cannot see, or one switched off for the hub (such as
`propose_hub_change` while in-app editing is off), is left out of the offered
list. The judge is a single call with no tools and no hub persona. It does not
see the Hubzoid runtime guidance added to every system prompt.

Rules you wrote in `AGENTS.md` apply to every judged case. Do not copy them
into a separate rules file, where they would drift. To apply a rule to many
cases, put it in `AGENTS.md`, where the agent also follows it.

The judge model is chosen in this order: `--judge-model`, then
`HUBZOID_EVAL_JUDGE_MODEL`, then the hub's own model. Pin it for anything you
track over time:

```bash
HUBZOID_EVAL_JUDGE_MODEL=claude-local/opus
```

A model tends to rate its own output generously, and if the hub's model
changes, unpinned scores change with it.

If the judge itself fails, for example when rate limited or when its reply
cannot be read, the case reports a judge error, separate from a failed case.

## Required evals in doctor

A hub can say which evals must pass before it is ready, in
`<hub>/quality/manifest.yaml`:

```yaml
version: 1
required: [refund-window, approvals/*]   # case ids, or globs over them
required_tags: [safety]                  # every case with one of these tags
```

`hubzoid doctor` then shows one `eval.<id>` row per required case:

| Row | Meaning |
|---|---|
| `ok` | Its latest result passed, for the case and the hub as they are now |
| `fail` | Its latest result failed (`hubzoid eval explain` shows why), or the case file cannot be read |
| `pending` | It never ran, it last ran with `--no-judge` although it has criteria, or the case file or the hub changed since it ran. Run it again |

A hub change here means its instructions, skills, knowledge, tools, workflows,
settings or configuration. Configuration means:

- the names in the hub's `.env` and `restricted/.env`, and the values of the
  ones that are not secret
- the model the hub runs on (`MODEL` from the environment or `.env`, or the
  run's `--model`). Switching the model makes results pending. Rotating an API
  key does not
- the files that change what the agent may do: approval rules (`hitl.yaml`),
  budgets (`budgets.yaml`), sandboxes (`sandbox.yaml`, `sandboxes/`) and write
  modes (`work-items.yaml`)

Other files, such as branding or saved output, do not make results pending.

A requirement that matches no case fails the `evals.quality` row, so a typo
never reads as satisfied. A pending case never counts as passing. Doctor exits
1 until every required eval is `ok`.

## Run in CI

```yaml
- run: hubzoid eval run ./my-hub --required   # the release gate
- run: hubzoid eval run ./my-hub              # everything, judged
```

A non-zero exit fails the build. `--required` runs exactly the required cases
and exits 2 when the gate cannot be trusted. The full list of conditions and a
ready workflow file are in [CI for a hub](ci.md). Keep `.hubzoid/evals/*.json`
as a build artifact.

## Running on a schedule

Add a cron to any case and it runs inside `hubzoid run`:

```markdown
---
schedule: "0 6 * * 1"     # Mondays at 06:00, server time
tags: [canary]
---
```

Scheduled evals use the same scheduler as `schedule/*.md`: the same 5-field
cron, catch-up after downtime, idle gate (an eval never starts while someone is
mid-conversation) and one run at a time. Cases due on the same tick run as one
suite. Results are saved before a failure is reported to workflow monitoring.

Failures are logged at `ERROR` and show in `hubzoid eval status`.
`HUBZOID_DISABLE_SCHEDULE=1` turns off scheduled tasks and scheduled evals
together.

| Trigger | Scope | Catches |
|---|---|---|
| CI, on push (`trigger: ci`) | everything | a change to `AGENTS.md`, a skill or a tool |
| `schedule:` | a few canary cases | drift you did not cause: a provider updates a model, a data source shifts |
| A scheduled task that commits and pushes | everything, through CI | a `schedule/*.md` job that rewrote `knowledge/` |

## Results

Every run writes `<hub>/.hubzoid/evals/<timestamp>_<id>.json` with the full
responses. That file is the record, the CI artifact and what `--compare` reads.
The newest 200 runs are kept. `HUBZOID_EVAL_KEEP_RUNS=0` keeps all.

Each results file records `hubzoid_version` and, when the hub folder is in a
Git work tree, `git_commit` and `git_dirty` (uncommitted changes in the hub
folder). The JUnit report carries them as properties. It has one test case per
eval, a failed check or judge verdict as a failure, and a crash or timeout as
an error.

`hubzoid eval explain <hub> <case>` prints the prompt, the full response, the
tools called, every check's verdict, the judge's reasoning, and the paths to
the case file and `AGENTS.md`. Editing instructions, not code, is the usual fix.

`--compare` prints only what moved:

```
$ hubzoid eval run my-hub --compare
REGRESSIONS: 1
  policy-exception            PASS → FAIL  missing: "14 days"
  tone-check                  FAIL → PASS
```

Renaming a case file reads as one case removed and one added.

Tool details in results hold the name, arguments, success or failure, time and
a preview of up to 500 characters. Saved arguments, previews, errors,
transcripts and judge reasoning are redacted for secret key and token patterns.
That cannot catch every secret written in prose, so use synthetic data and
protect the hub's backups. `.hubzoid/` stays outside Git.

Results files use schema 2. Schema 1 files stay readable and unknown future
schemas are rejected. `index.json` is a summary that can be rebuilt.

### In the Console

The agent's Evals tab is read-only. It shows the latest run's passed of total,
its time and trigger, a bar for each of the last 10 runs, the cases failing now
and those never run. Each case shows its last 10 results. In Settings, Agents,
an agent with cases shows **Evals 8/10** or **Evals not run**. Case names and
pass or fail results are shown for every case. The details of a run with
`run_as` belong only to that account, which also needs Settings or agent
access. Other viewers see a private result notice. There is no run button. Run
evals with the CLI, from CI or on a schedule.

### Send runs to Langfuse

Everything above works with nothing else installed. If the hub already has
tracing set up ([Observability](OBSERVABILITY.md)), eval runs are also pushed:

```bash
HUBZOID_OTEL_ENDPOINT=https://langfuse.internal/api/public/otel
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Keys are also read from `OTEL_EXPORTER_OTLP_HEADERS`. Each case becomes a trace
in a per-hub dataset with one score per check, tagged `hubzoid.eval` so you can
keep eval traffic out of production dashboards. A failed push never fails a
run. The local JSON is the record.

## What a run costs

Every case runs the hub's agent, which is a real model call with tools and
knowledge lookups. Only the second call varies:

| Case | Model calls |
|---|---|
| No `## Criteria` | 1 (the agent) |
| With `## Criteria` | 2 (the agent, then the judge) |
| Any case, with `--no-judge` | 1 (the agent) |

`--no-judge` roughly halves the cost of a judged suite. It does not make a run
free. To keep cost down, use `--case` or `--tag` while you iterate, add
`## Criteria` only where judgement is needed, and schedule a small set of
canary cases while CI runs the rest.

Cases run one after another, so a run takes the sum of its cases. A long
answer can take a minute or more.

## Writing good cases

Use `contains` and `not_contains` for facts that must or must not appear word
for word. Use `## Criteria` for anything that needs judgement. When a text
check and the judge disagree, the text check is usually wrong. Three checks
that failed a correct answer:

- `contains: ["whoami"]` when `AGENTS.md` asks for tools described in plain
  language, so the agent wrote "Who Am I".
- `not_contains: ["e.g."]` as a test for hedging.
- Criteria ending "and nothing beyond it", which failed a reply that added one
  helpful sentence.

Cover every branch. If a tool can return a link, details or an error, say what
a correct answer looks like for each. Say what a failure is, not only what a
pass is. "Inventing setup steps the tool did not return is a failure" grades
more consistently than "reports what the tool returned".

## Limits

- Cases run one at a time. Hub tools touch real systems, so a parallel suite
  would make failures depend on order.
- Evals use the hub's real credentials and tools. A case that calls a
  write-capable tool really writes unless it uses
  [fake tool results](#fake-tool-results). Use fakes, read-only prompts,
  dedicated test accounts or a sandboxed hub for scheduled evals.
- For model-free runs, put `MODEL=hubzoid-test/scripted` in a test hub's own
  `.env`.
- A case's id is its path under `evals/` without `.md`, so
  `evals/approvals/reply-reject.md` is `approvals/reply-reject`. Files and
  folders starting with `_` or `.`, and `evals/fixtures/`, are not cases.

## Troubleshooting

**`no tool named <name> in this agent`.** The name in `expect_tools`,
`forbid_tools` or `expect_tool_args` is not a tool this agent has. Check the
spelling and use the short name. See [tool names](#tool-names).

**Exit 2 with a file named.** A case file cannot be read, or has an unknown
frontmatter key. The message names the file and the key.

**Judge error.** The grading call failed, not the hub. Run the case again, or
pin a different judge with `--judge-model`.

**`pending` in doctor.** The case, the hub or its model changed since the last
run. Run `hubzoid eval run <hub> --required`.
