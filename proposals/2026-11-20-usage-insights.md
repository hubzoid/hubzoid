# Usage insights

Founder-approved 20 Nov 2026 for 2.0.9, after review of the screen design
(`HubzoidAgent/docs/usage-screen-design-2026-11-20.html`, sample data) and the
suggestions doc (`HubzoidAgent/docs/usage-page-metrics-suggestions-2026-11-20.md`).
Builds on the usage period fix (09c97874).

## Problem

The Usage page shows five totals and a per-agent row. An admin cannot see
where usage comes from (chat, schedules, MCP, evals), which channel or model
costs most, who uses it, how often calls fail, or how fast replies are.
Hubzoid already records most of this in `hz_usage` and shows little of it.
Cache tokens are folded into input tokens, so a large cached prompt looks
like a large bill. Reasoning tokens and time to first words are not recorded.

## Why now

Teams moving from one person to a team ask what the agents cost and whether
they work. The 2.0.9 period fix made the page consistent, so the rest can
build on one period control.

## What should happen

One page, one period control (24 hours, 7 days, 30 days) and an agent filter,
both driving every section. Monthly limit bars stay on the calendar month and
say so.

1. Totals: messages, people active, tokens (share read from cache), cost
   (calls without a price), workflow runs (failed, missed slots), error rate
   (denied tool calls). Each shows the change against the previous period of
   the same length, hidden when that period was not fully recorded.
2. Tokens and cost over time: hourly buckets for 24 hours, daily for 7 and
   30 days. Input, input read from cache, output. Hover details and a table
   view. Buckets before recording started are marked.
3. By channel (`surface`) and how it was used (`trigger`): calls, share, cost.
4. By agent: a sortable, searchable, paged table (messages, people, tokens,
   cost, errors, p50 response time, monthly limit). A row opens the limits
   editor as today. Settings › Agents uses the same search and pages and
   gains an errors column, always for 30 days.
5. Response time: time to first words in chat (p50), full reply or call p50
   and p95, and p50 and p95 per bucket.
6. By model: calls, input, share from cache, output, reasoning, cost, models
   without a price flagged. Six rows, then Show all.
7. By person: people and service subjects (schedules, workflows), searched,
   sorted and paged on the server. Admins only, scoped as `/summary` is.

Charts follow `HubzoidStudio/brand/visual.md`: neutrals carry the data, brand
orange marks one series (output tokens, p95, a hovered bar), warning and danger
only for state.

## Scope and non-goals

- No tool call counts and no per sub-agent or skill usage (later change).
- No 90-day or custom range, no daily rollup table, no export.
- No eval scores on this page (evals stay on the Evals screen).
- `/summary` keeps its shape. Only an additive `errors` per hub.
- No change to budgets, limits or how cost is estimated.

## Open questions

None blocking. Rows written before this change have NULL for the new columns:
they count in totals, and show as "Earlier" in how it was used.

## Definition of done

On a copy of a test hub with real chat, workflow, schedule and MCP use, the
Usage page shows each section with numbers that match `hz_usage`, switching
period and agent changes every section, search and pages work, and
Settings › Agents shows 30-day numbers with errors. Focused tests pass. A proof
page records it.

---

# Implementation plan

Branch `feat/usage-insights` on `f3fd8692` (release branch `feat/mcp-parity`).
Kakshil authors the feature, Darshika fixes and tests, Hubzoid the bundle
rebuild (AGENTS.md commit rules).

## Data: one migration, five nullable columns

`hubzoid/migrations/operational/versions/0083_usage_details.py`, revision
`op_0083`, down `op_0081`. `hz_usage` gains:

| Column | Type | Meaning |
|---|---|---|
| `trigger` | Text | chat, schedule, manual, event, mcp, eval. NULL for older rows |
| `cache_read_tokens` | BigInteger | Input tokens read from cache. Part of `input_tokens` |
| `cache_write_tokens` | BigInteger | Input tokens written to cache. Part of `input_tokens` |
| `reasoning_tokens` | BigInteger | Output tokens spent reasoning. Part of `output_tokens`. NULL when not reported (Claude) |
| `first_token_ms` | Integer | Chat only: time from the start of the turn to the first answer text |

`input_tokens` and `output_tokens` keep their meaning as totals, so budgets,
cost estimates and `/summary` are unchanged. Other branches that also descend
from `op_0081` need a merge revision when combined.

## Writers

`hubzoid/usage.py`:
- `record()` gains keyword arguments `trigger`, `cache_read_tokens`,
  `cache_write_tokens`, `reasoning_tokens`, `first_token_ms`, all optional.
- `usage_details(raw: dict) -> dict` picks those keys from a backend usage
  dict, so each call site passes `**usage_details(raw)` instead of new code.
- `run_tag(run_id, trigger=None)` also sets a trigger context variable.
  `record()` uses an explicit trigger, else the tagged one, else derives
  `mcp` for surface mcp, `chat` for other non-workflow surfaces, NULL for an
  untagged workflow row.
- Workflow triggers map: schedule and personal_schedule to schedule, operator
  and cli to manual, webhook and update to event, tool to mcp when the caller's
  surface is mcp, else chat.

Call sites:
- `workflows/context.py` run context: `run_tag(run_id, trigger=mapped)`.
- `schedule_runner.py`: `run_tag(run_key, trigger="schedule")`, and the round
  row passes `**usage_details(raw)`.
- `runtime.py` (call_agent, call_llm, stations), `jev.py`, `mcp_parity.py`
  (trigger mcp), `evals/runner.py` and `evals/judge.py` (trigger eval),
  `server._record_turn` (chat turns): pass `**usage_details(...)`.
- Backends fill the keys where the provider reports them:
  - Claude (`factory_claude.claude_usage`): `cache_read_input_tokens`,
    `cache_creation_input_tokens`. No reasoning split (thinking is inside output).
  - OpenAI Agents SDK (`runtime._record_openai_usage`):
    `input_tokens_details.cached_tokens`, `output_tokens_details.reasoning_tokens`.
  - Codex (`factory_codex`, `thread/tokenUsage/updated`): `cachedInputTokens`,
    `cacheWriteInputTokens`, `reasoningOutputTokens`, summed across records.
- First words: `chat/runs.py` notes the monotonic time of the first answer
  text and `_finish` passes `first_token_ms`. The OpenAI-compatible stream in
  `server.py` does the same for the first text delta.

## API

`hubzoid/usage.py` gains `insights(engine, hubs, since, until, bucket)` and
`people(engine, hubs, since, q, sort, offset, limit)`. SQL is plain and works
on SQLite and Postgres (bucket index uses `CAST` on SQLite and `FLOOR` on
Postgres). Percentiles are computed in Python from the period's
`duration_ms` and `first_token_ms` values.

`hubzoid/portal.py`, admin only, hubs from `allowed_hubs(admin)`:
- `GET /portal/api/usage?period=7d&hub=<key>`: `totals` (messages, chats,
  people, calls, errors, cancelled, input, output, cache_read, cache_write,
  reasoning, cost, unpriced), `previous` (same totals for the previous window,
  or null when `recording_since` is after its start), `series` (bucket start,
  input, cache_read, output, cost, p50, p95), `by_surface`, `by_trigger`,
  `by_model`, `by_hub` (with errors, people, p50, p95), `latency` (first
  words p50, p50, p95), `recording_since`, `bucket`. Unknown hub: 404.
- `GET /portal/api/usage/people?period=7d&hub=&q=&sort=cost&page=0&size=8`:
  `rows` (subject, kind person or schedule or workflow, agents, messages,
  tokens, cost, last), `total`. Sort is one of cost, tokens, messages, name.
- `/summary` hub rows gain `errors` (status error in the period).

## Web app

- `portal/src/screens/UsageScreen.tsx`: the design, in the existing `hz-`
  styles. Small SVG chart components in `portal/src/components/UsageCharts.tsx`
  (stacked bars, lines, sparkline) with hover tooltips and a table view. No
  chart library added. Agent filter and period live in the URL (`period`,
  `agent`).
- `portal/src/screens/AgentsScreen.tsx` (`AgentGrid`): pages of 10 and an
  "Errors, 30 days" column. Search and filters already exist.
- `portal/src/api.ts`: `UsageInsights`, `UsagePeople`, `SummaryHub.errors`.
- `portal/src/app/app.css`: usage chart, table and pager styles.
- Rebuild `hubzoid/portal_dist` once at the end.

## Tests (focused only, full suite left to CI)

- `tests/test_usage_insights.py`: rows across buckets and periods, totals,
  series sums match totals, previous period null and present, trigger
  mapping and NULL as earlier, model cache and reasoning sums, percentiles,
  hub filter and scope (a delegate sees only their hubs, an unknown hub is
  404), people search, sort and pages.
- Writers: `claude_usage` cache keys, OpenAI and Codex detail extraction,
  `run_tag` trigger, chat `first_token_ms`, migration upgrade on an existing
  hub (columns added, old rows readable).
- Existing: `tests/test_console_home.py`, budgets tests, usage tests.
- Browser: `portal/tests/app-settings.cjs` and `journey.cjs` with fixture
  endpoints for `/usage` and `/usage/people` (period and agent switching,
  search, pages, table view, Settings › Agents errors column).

## End-to-end

A copy of `HubzoidTestHub/mcp-parity-e2e` on its own ports, with the branch
code: real web chat turns (Claude), a manually run workflow, a scheduled run,
an MCP call from Claude Code, an eval if quick. Then the Usage page in the
real web app: every section, 24 hours, 7 days, 30 days, agent filter, search
and pages, Settings › Agents. A few existing rows moved back in time so the
periods differ, labelled as such. Proof page with screenshots and the
matching `hz_usage` queries.

## Blast radius

Medium. One additive migration, optional keyword arguments on `usage.record`,
two new read-only admin endpoints, one additive `/summary` field, and the two
web app screens. No change to budgets, chat or workflow behaviour. Risk is in
the writers: every call site must stay best effort (usage never breaks work).

## Plan review (Codex gpt-6.1-sol high, 20 Nov 2026)

Seven findings, all checked against the code and accepted:

1. Workflow runs, failed runs, missed slots and denied tool calls are not usage
   rows. They stay on `/summary` (per hub, so the agent filter picks the hub's
   row) and show no change against the previous period. The error rate is
   errors over recorded calls, labelled so, with denials beside it.
2. Claude one-shot and station helpers whitelist four usage keys, and direct
   LiteLLM calls and eval judging build fresh dicts. All four now carry the
   cache and reasoning keys (`usage.litellm_details` for OpenAI-style usage).
3. A workflow started from chat or MCP ran as `operator` with no surface.
   `control.start_now` and report tools now put `via` (the asking surface) in
   the run envelope, and `usage.run_trigger` maps it to chat or mcp.
4. Markdown tasks started by hand or by events were labelled schedule. The
   origin now comes from the run id's slot (`usage.markdown_origin`). Work
   item runs count as manual.
5. The OpenAI-compatible stream mixes thinking and tool text into its deltas,
   so `first_token_ms` is recorded only in web chat, from typed answer items,
   and passed with the turn's usage.
6. A failed Codex turn recorded `ok`. Its usage now carries `status: error`.
   Calls that fail before any usage is recorded still write no row, so the
   page says "of N recorded calls".
7. A Claude turn's row includes its background and sub-agent calls under the
   answering model. The model card says "by the model that answered".

Column `trigger` is named `origin` in the table (TRIGGER is an SQL keyword).
The API keeps `by_trigger`.
