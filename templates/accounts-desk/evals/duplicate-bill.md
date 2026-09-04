---
expect_tools: [search_bills]
timeout: 180
---
## Prompt
Is INV-2288 from Kestrel Logistics for 12,400 a duplicate?

## Criteria
Searches the ledger and finds two bills numbered INV-2288 from Kestrel
Logistics for 12,400, dated 2026-08-28 and 2026-09-01. States that this is a
suspected duplicate under the same-supplier, same-bill-number rule, marks it
as an exception for the accounts team lead, and does not draft an entry for
the second bill. Declaring it clean because the PO matches is a failure.
