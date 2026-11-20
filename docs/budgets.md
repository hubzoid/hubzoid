# Usage limits

Limit how much a hub's agents use: tokens, turns or dollars, for each run, each
work item and the whole hub per month. Nothing is limited until you add
`budgets.yaml` to the hub folder.

```yaml
version: 1
limits:
  run:          # one chat reply, or every model call of one workflow run
    max_turns: 40
    max_output_tokens: 100000
    max_cost_usd: "2.00"
  work_item:    # every run of one work item, including its isolated agents
    max_output_tokens: 300000
    max_cost_usd: "5.00"
  month:        # everything this hub does in a UTC calendar month
    max_cost_usd: "40.00"
```

| Setting | Scopes | What it limits |
|---|---|---|
| `max_input_tokens` | `run`, `work_item`, `month` | Input tokens. |
| `max_output_tokens` | `run`, `work_item`, `month` | Output tokens. |
| `max_cost_usd` | `run`, `work_item`, `month` | Dollars, written as text such as `"2.00"`. `"0"` blocks paid work. |
| `max_turns` | `run` only | The turns of one agent call (one chat reply, or each `hub.call_agent` and isolated agent call of a workflow). On Codex it counts tool calls, the limit Codex exposes. |

Leave out anything you do not want to limit. Limits are per hub. Per person and
per agent limits are not available.

## Set a monthly limit

1. Add `budgets.yaml` with a `month` scope, as above.
2. Check it: `hubzoid budgets validate ./hub --file budgets.yaml`.
3. Apply it: `hubzoid budgets apply ./hub --file budgets.yaml --expected-revision absent`
   for a new file, or the revision `hubzoid budgets show ./hub` prints.

You can also open the agent's row in **Settings → Usage**, edit the limits, and
use Check and Save.

## How limits work

- **Checked first.** Before every model call (chat replies on every channel,
  `hub.call_llm`, `hub.call_agent`, isolated agents, scheduled tasks) Hubzoid
  adds up what was used in each scope that applies. If one is used up, the call
  does not start and nothing is charged. In chat the person reads, for example,
  "The limit of 40.00 USD for this hub this month is used up".
- **Shared fairly.** A run with a run limit holds that much (or what is left,
  when less) while it runs, so runs at the same time cannot together go past a
  limit. Without a run limit, runs already going when a limit fills up may each
  finish.
- **Stopped inside a run.** The runtime's own caps are set from what is left:
  Claude's `max_turns` and `max_budget_usd`, the turn limit on Codex and the
  OpenAI Agents runtime, a cost check after each model call on the OpenAI Agents
  runtime, and an output token bound on single completions. A reply stopped
  there says so. The person replies "continue" to start a new run, which is
  checked again and can never go past a work item or monthly limit. Token limits
  stop the next call, not one that is already running.
- **Usage that never reported.** A call's hold is settled in the same write as
  its usage record. If Hubzoid stops in the middle of a call, or the record
  cannot be written, the hold keeps counting (across a month boundary too) until
  an administrator checks the provider's usage and releases it.
- **Everything counts.** Chat on every channel, workflows, Jev, isolated agents,
  scheduled tasks (all rounds of one run are one run) and evals, including their
  judge. Isolated agents and their sandboxes count through the work item and
  month.

## How cost is counted

| Model | Dollar limits |
|---|---|
| `claude-local` and Claude on [Amazon Bedrock](bedrock.md) | Apply. Claude reports the cost of each run, and a resumed chat records each turn's own cost. |
| A model in LiteLLM's price table | Apply. Priced from the table pinned with the installed LiteLLM, never one fetched at start up. |
| `codex-local` | Skipped, since a Codex login has no per call charge and nothing is counted in dollars. Needs a turn or token limit that applies to the call, or it is refused (`unpriced_model`). |
| Any other model with no price, for example an OpenRouter model missing from the table | Refused while a dollar limit applies (`unpriced_model`), because its calls still cost money. |

Subscription logins work with token and turn limits. `hubzoid budgets show`
says which rule applies to the agent's model.

On Bedrock, if the Claude CLI reports no cost for a call, the row's cost stays
empty. A scope with a dollar limit that holds such a row then refuses further
Bedrock calls (`unpriced_usage`), because the limit can no longer be checked.
Use token or turn limits on Bedrock to avoid this.

## See usage

**Settings → Usage** shows usage for the period you choose (24 hours, 7 days or
30 days, 7 days by default). Each agent's row has its monthly limit bar, which always counts the
calendar month, whatever period is shown. Opening the row shows the limits as
fields, the file with Check and Save, and reservations that never reported. See
[the Usage page](ADMINISTRATION.md#the-usage-page).

## Commands and API

```bash
hubzoid budgets show ./hub
hubzoid budgets validate ./hub --file budgets.yaml
hubzoid budgets apply ./hub --file budgets.yaml --expected-revision absent
hubzoid budgets release ./hub RESERVATION --reason "Provider shows no charge."
```

`show` prints the revision to pass to `apply`. The API is
`GET|PUT /portal/api/hubs/{hub}/budgets`, `POST .../budgets/validate` and
`POST .../budgets/reservations/{id}/release`. Reading needs `workflows_view`,
changes need `workflows_manage`.

## Troubleshooting

| Code | What to do |
|---|---|
| `budget_exhausted` | Wait for the next month, or raise the limit in budgets.yaml. |
| `unpriced_model` | For `codex-local`, add a turn or token limit. For another model with no price, remove the dollar limits or use a priced model. |
| `unpriced_usage` | A Bedrock call recorded no cost. Use token or turn limits on Bedrock, or ask an administrator to review the usage. |
| `invalid_budgets` | Fix the file at the line shown. |
| `revision_conflict` | Someone changed the file. Reload, review and apply again. |
| `hub_read_only` | The agent's files come from its Git repository. Change budgets.yaml there and redeploy. |
