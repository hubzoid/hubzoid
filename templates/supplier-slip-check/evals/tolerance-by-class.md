---
expect_tools: [read_knowledge]
forbid_tools: [post_to_team_chat, supplier_slips, ledger_receipts]
timeout: 120
---
## Prompt
What is the tolerance for weighed bulk materials, and does it apply to boxes?

## Criteria
Reads the reconciliation rules and states the weighed tolerance as 0.5
percent of slip quantity or 5 kg, whichever is larger. Says clearly that
boxes are counted items with zero tolerance, so the weighed tolerance does
not apply. Does not call the data tools for a policy question.
