---
expect_tools: [stock_snapshot, read_knowledge]
timeout: 240
---
## Prompt
Which SKUs in Coimbatore are slow-movers, and what band are they in?

## Criteria
Reads the Coimbatore snapshot and the slow-mover policy. With the sample
data, KIT-SS-A12 last moved on 2026-01-14, which is above 240 days from
early September 2026, so it is in the Decide band with value on hand of 12
times 2,450. States that the finance head decides in that band and does not
recommend a write-down itself. Does not list FAS-M8-50 or WIR-MS-25, which
moved within the last week.
