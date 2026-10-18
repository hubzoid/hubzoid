# Usage limits

Hubzoid can limit how much a hub's agents use: tokens, turns or dollars, for
each run, each work item and the whole hub per month. Nothing is limited until
you add `budgets.yaml` to the hub folder.

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

Each scope takes `max_input_tokens`, `max_output_tokens` and `max_cost_usd`.
`run` also takes `max_turns`. Leave out anything you do not want to limit.
Amounts are text such as `"2.00"`; `"0"` blocks paid work.

## How it works

- **Checked first.** Before every model call (chat replies on every channel,
  `hub.call_llm`, `hub.call_agent`, isolated agents, scheduled tasks) Hubzoid
  adds up what was used in each scope that applies. If one is used up, the
  call does not start and nothing is charged. In chat the person reads, for
  example, "The limit of 40.00 USD for this hub this month is used up".
- **Shared fairly.** A run with a run limit holds that much (or what is left,
  when less) while it runs, so runs at the same time cannot together go past
  a limit. Without a run limit, runs already going when a limit fills up may
  each finish.
- **Stopped inside a run.** The runtime's own caps are set from what is left:
  Claude's `max_turns` and `max_budget_usd`, and the turn limit on Codex and
  the OpenAI Agents runtime. A reply stopped there says so; the person replies
  "continue" to start a new run, which is checked again and can never go past
  a work-item or monthly limit. Token limits stop the next call, not one that
  is already running.
- **Prices.** Claude reports the cost of each run. Other models are priced
  from LiteLLM's price table. Subscription logins work with token and turn
  limits. A model with no price (a Codex login) is refused while a dollar
  limit applies (`unpriced_model`) rather than run without a bound.
- **Usage that never reported.** If Hubzoid stops in the middle of a call, the
  amount that call held keeps counting until an administrator checks the
  provider's usage and releases it.

## Commands

```bash
hubzoid budgets show ./hub
hubzoid budgets validate ./hub --file budgets.yaml
hubzoid budgets apply ./hub --file budgets.yaml --expected-revision absent
hubzoid budgets release ./hub RESERVATION --reason "Provider shows no charge."
```

`show` prints the revision to pass to `apply`. The Console's **Usage limits**
page shows the same: limits in force, this month's usage, the file with Check
and Save, and reservations that never reported. The API is
`GET|PUT /portal/api/hubs/{hub}/budgets`, `POST .../budgets/validate` and
`POST .../budgets/reservations/{id}/release`. Reading needs `workflows_view`,
changes need `workflows_manage`.

| Code | What to do |
|---|---|
| `budget_exhausted` | Wait for the next month, or raise the limit in budgets.yaml. |
| `unpriced_model` | Use token or turn limits for this model. |
| `invalid_budgets` | Fix the file at the line shown. |
| `revision_conflict` | Someone changed the file; reload, review, apply again. |

## Limits

- Per-person and per-agent limits are not available; limits are per hub.
- Isolated agents and their sandboxes count through the work item and month.
