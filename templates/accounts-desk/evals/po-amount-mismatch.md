---
tags: [canary]
expect_tools: [find_purchase_order]
timeout: 180
---
## Prompt
Check bill INV-2291 from Arcadia Packaging for 48,600 against PO-1187.

## Criteria
Looks up PO-1187 and reports that the PO total is 45,000, so the bill is
3,600 over, which exceeds the tolerance of the smaller of 2 percent or 500.
Gives the verdict Exception, not Matched. Names who decides (difference up
to 25,000, so the accounts team lead). Does not draft a posting entry for an
exception. Rounding the difference away or calling it a match is a failure.
