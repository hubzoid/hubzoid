---
schedule: "5 8 * * 1"         # Mondays at 08:05, local server time
timeout: 600
max_rounds: 2
max_turns: 20
write: ["output/notes/"]
---

Write the short weekly numbers note the team reads on Monday morning.

- Call `current_time` for today's date. The note file is
  `output/notes/numbers-<YYYY-MM-DD>.md`.
- Your state file records the date of the last note. If this week's file is
  recorded, report DONE.
- Call `list_metrics()` and, for the latest month available, fetch
  `revenue`, `gross_margin_pct`, `cash_balance`, `receivables_overdue`,
  and `orders`. Fetch the previous month for `revenue` and `orders` too.
- Write six lines at most: one per metric with value, period, and change
  from the previous month where you fetched it, following
  `read_knowledge('metric-definitions')` for the arithmetic. End with
  "Source: metric store, <period>".
- Save with `write_hub_file` and record the date in the state file. This
  file is the deliverable; posting to a channel is a placeholder in this
  template.
- You are done when the file is written and the state file is updated.
