---
schedule: "23 6 * * 1-5"      # weekdays at 06:23, local server time
timeout: 900
max_rounds: 3
max_turns: 30
write: ["output/briefings/"]
---

Write this morning's leadership briefing and save it.

- Call `current_time` to get today's date. The briefing file is
  `output/briefings/<YYYY-MM-DD>.md`.
- Your state file records the date of the last briefing written. If today's
  file already exists and the state file says it was written, do nothing
  more and report DONE.
- Call `sales_snapshot('yesterday')`, `sales_snapshot('mtd')`,
  `cash_position()`, and `ops_snapshot()`.
- Read `read_knowledge('briefing-format')` and `read_knowledge('company-context')`
  and write the briefing exactly in that format, thresholds applied.
- If any tool fails, write the briefing with a clear line saying which
  section is missing and why. Never fill a missing section with guesses.
- Save the briefing with `write_hub_file`, then record today's date in your
  state file.
- Delivery to the leadership channel is a placeholder in this template. The
  file under `output/briefings/` is the deliverable until your team adds a
  posting tool in `tools_local/`.
- You are done when the file is written and the state file is updated.
