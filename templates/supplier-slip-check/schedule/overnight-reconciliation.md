---
schedule: "17 2 * * *"        # every day at 02:17, local server time
timeout: 1200
max_rounds: 4
max_turns: 40
write: ["output/reconciliation/"]
---

Reconcile yesterday's supplier slips against the goods receipt ledger and
tell the operations team about every mismatch.

- Call `current_time` and work out yesterday's date.
- Your state file records the last date fully reconciled. Reconcile every
  date after it up to and including yesterday, oldest first, so a missed
  night is caught up. On the very first run, reconcile yesterday only.
- For each date, call `supplier_slips(date)` and `ledger_receipts(date)`.
  Apply `read_knowledge('reconciliation-rules')` exactly: pair by slip
  number, then check supplier, material, unit, and quantity within the
  tolerance for that material class.
- Write `output/reconciliation/<date>.md` with `write_hub_file`: the
  mismatch lines first, then the matched count.
- Compose the chat post in the wording from
  `read_knowledge('escalation-rules')` and call `post_to_team_chat` once per
  date. With zero mismatches, post the one-line count only.
- Record the date in your state file after its post is sent.
- If a tool fails for a date, write what you have, say in the post which
  tool failed, and do not record that date as reconciled.
- You are done when the state file's last reconciled date is yesterday.
