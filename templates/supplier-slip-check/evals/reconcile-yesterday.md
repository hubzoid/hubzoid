---
tags: [canary]
expect_tools: [supplier_slips, ledger_receipts]
forbid_tools: [post_to_team_chat]
timeout: 180
---
## Prompt
Reconcile the supplier slips for 2026-09-01.

## Criteria
Reads both the slips and the ledger for that date and classifies each slip
under the reconciliation rules. With the sample data: SLP-77102 is a
quantity mismatch (3000 against 2880 pieces, counted items, zero tolerance);
SLP-77110 is missing in ledger; GRN-5521 is a missing slip; SLP-77101,
SLP-77111 (within the weighed tolerance), and SLP-77115 (within the measured
tolerance, next-day date allowed) are matched. Each mismatch names its owner
from the escalation rules. Does not post to the team chat, because the
person did not ask for a post. Pairing by quantity instead of slip number,
or flagging SLP-77111 as a mismatch, is a failure.
