---
schedule: "41 20 * * 1-6"     # Monday to Saturday at 20:41, local server time
timeout: 1200
max_rounds: 4
max_turns: 40
write: ["output/accounts/"]
---

Check every pending supplier bill and write tonight's exceptions report.

- Call `current_time` for today's date. The report file is
  `output/accounts/exceptions-<YYYY-MM-DD>.md`.
- Your state file records, per bill number and date, the verdict you last
  gave. A bill already checked with an unchanged total keeps its verdict;
  do not re-check it. Anything new or changed is checked in full.
- Call `pending_bills()`. For each bill, run the four checks in the order
  the AGENTS.md sets out: purchase order, amount and quantity within the
  tolerances from `read_knowledge('matching-rules')`, duplicates via
  `search_bills`, supplier standing via `supplier_profile`.
- Write the report in three parts: exceptions first (bill, supplier, amount,
  the rule broken, the exact difference, who decides), then matched bills
  with their draft entries in the shape from
  `read_knowledge('chart-of-accounts-conventions')`, then anything you could
  not check and why.
- Exceptions older than three working days go at the top, marked as such.
- Save with `write_hub_file`, then update the state file with every verdict.
- You are done when every pending bill has a verdict in the report and the
  state file is current.
