---
name: escalation-rules
description: Who owns each supplier, the wording of the team chat post, and when a mismatch escalates.
keywords: [escalation, owner, chat post, supplier owner]
---

# Escalation rules (sample)

## Supplier owners

| Supplier | Owner | Backup |
|---|---|---|
| Arcadia Packaging | Pune warehouse lead | Operations head |
| Sundaram Wire Works | Coimbatore warehouse lead | Operations head |
| Kestrel Logistics | Ahmedabad warehouse lead | Operations head |
| Any supplier not listed | Operations head | none |

## When a mismatch escalates

- A quantity mismatch above 10 percent of the slip quantity goes to the
  owner and the operations head together.
- A supplier with three or more mismatches in seven days is named at the top
  of the post with the count.
- "Missing in ledger" older than one day goes to the operations head.

## Wording of the team chat post

Subject line, then one line per mismatch, then a closing count. Nothing else.

```
Slip check for 2026-09-01: 3 mismatches, 41 matched
SLP-77102 · Arcadia Packaging · quantity mismatch · slip 3000 pcs, ledger 2880 pcs, short 120 · Pune warehouse lead
SLP-77110 · Sundaram Wire Works · missing in ledger · 480 kg wire coil · Coimbatore warehouse lead
GRN-5521 · Kestrel Logistics · missing slip · ledger 2 consignments, no slip · Ahmedabad warehouse lead
41 slips matched. Report: output/reconciliation/2026-09-01.md
```

Nothing goes to chat when there are zero mismatches except the one-line
count.
