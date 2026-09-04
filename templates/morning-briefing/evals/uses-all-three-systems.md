---
tags: [canary]
expect_tools: [sales_snapshot, cash_position, ops_snapshot]
forbid_tools: [web_search, http_get]
timeout: 180
---
## Prompt
Give me this morning's briefing.

## Criteria
Calls all three system tools and follows the five-section order from the
briefing format: headline, sales, cash, operations, one action. Names the
source tool and as-of time per section. Applies the thresholds from the
knowledge file rather than its own judgement: with the sample data, the
overdue Brightline Packaging receivable (34 days, 310,000) and the
Coimbatore incident open beyond 24 hours must be flagged. Recommends at most
one action with an owner. Inventing a figure that no tool returned is a
failure.
