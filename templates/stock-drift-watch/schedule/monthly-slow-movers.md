---
schedule: "11 6 1 * *"        # first of the month at 06:11, local server time
timeout: 1200
max_rounds: 4
max_turns: 40
write: ["output/stock/"]
---

List the slow-movers at every location and write the monthly report.

- Call `current_time` for today's date. The report file is
  `output/stock/slow-movers-<YYYY-MM>.md`.
- Your state file records the last month reported. If this month is
  recorded, report DONE.
- Call `locations()`, then `stock_snapshot(location)` for each. Days
  without movement is the gap between last_movement and today.
- Apply `read_knowledge('slow-mover-policy')`: place each SKU with stock on
  hand in its band, apply the exclusions, and total value on hand per band
  and per location.
- For every SKU in the "Move" band, call `sku_movement(sku, 120)` and note
  whether another location consumed it.
- Write the report with `write_hub_file`: totals first, then one table per
  band with the deciding role from the policy. Never recommend a write-down
  yourself; list the candidates for the finance head.
- Record the month in the state file. You are done when the file is written
  and the state file is updated.
