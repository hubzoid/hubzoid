---
schedule: "37 5 * * 1"        # Mondays at 05:37, local server time
timeout: 1200
max_rounds: 4
max_turns: 40
write: ["output/stock/"]
---

Scan every location for stock drift and write this week's drift report.

- Call `current_time` for today's date. The report file is
  `output/stock/drift-<YYYY-MM-DD>.md`.
- Your state file records the date of the last drift report. If this
  week's file exists and the state file confirms it, report DONE.
- Call `locations()`, then `stock_snapshot(location)` for each one.
- Apply `read_knowledge('drift-thresholds')`: classify every SKU's drift
  into its action row, mark stale counts, and rank the locations by total
  absolute drift value.
- For every SKU in the "recount within 2 days" row, call
  `sku_movement(sku, 30)` and add one line on what the movements suggest.
- Write the report with `write_hub_file`: the location ranking first, then
  the drift table sorted by value, then stale counts, then one line naming
  the location to count first.
- A location whose snapshot fails is listed as missing at the top. Do not
  omit it.
- Record today's date in the state file. You are done when the file is
  written and the state file is updated.
