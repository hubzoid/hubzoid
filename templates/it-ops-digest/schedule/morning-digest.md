---
schedule: "3 7 * * *"         # every day at 07:03, local server time
timeout: 900
max_rounds: 3
max_turns: 30
write: ["output/digests/"]
---

Write the hand-over digest for the on-call engineer coming on shift.

- Call `current_time` for today's date. The digest file is
  `output/digests/<YYYY-MM-DD>.md`.
- Your state file records the date of the last digest. If today's is
  recorded, report DONE.
- Call `alerts_since(12)` and `open_incidents()`.
- Follow `read_knowledge('handover-format')` exactly: counts, fired,
  suppressed with the rule that acted, open at hand-over with stale
  marking, first thing to do.
- For every open incident, call `runbook(alert_name)` and quote the first
  two steps under it. If no runbook exists, write "no runbook" and the file
  to edit.
- Save with `write_hub_file` and record today's date in the state file.
  Delivery to the on-call channel is a placeholder in this template; the
  file is the deliverable until a posting tool is added to `tools_local/`.
- You are done when the file is written and the state file is updated.
