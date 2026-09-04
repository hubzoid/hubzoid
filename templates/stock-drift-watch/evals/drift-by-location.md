---
tags: [canary]
expect_tools: [stock_snapshot]
timeout: 240
---
## Prompt
Where is stock drifting this week, and which location should we count first?

## Criteria
Reads the snapshot for every location and applies the drift thresholds from
the knowledge file. With the sample data, Coimbatore's FAS-M8-50 (drift of
22 units, above 5 percent) and Ahmedabad's FAS-M12-80 (19 units, above 5
percent) land in the recount-within-2-days row, and Pune's KIT-SS-A12 is a
class A item so any drift means a recount within 2 days. Pune's BRG-6205
is marked count overdue (last count older than 90 days). Ranks locations by
total absolute drift value and names one to count first with a one-line
reason. Inventing a SKU or a count, or recommending a write-off, is a
failure.
