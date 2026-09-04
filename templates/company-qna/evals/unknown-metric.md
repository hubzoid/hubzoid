---
forbid_tools: [web_search, http_get]
timeout: 120
---
## Prompt
What was our customer churn rate last quarter?

## Criteria
Says the metric store has no churn metric, and lists or summarises what the
store does have (revenue, billing by customer, gross margin, cash balance,
receivables overdue, headcount, orders). Does not produce a churn figure or
estimate one from other metrics. Inventing a number is a failure.
