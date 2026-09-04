---
name: metric-definitions
description: What each metric in the store means, how it is computed, and its period grain. Sample definitions, replace with your own.
keywords: [metrics, definitions, revenue, margin, billing, headcount, cash]
---

# Metric definitions (sample)

The metric store holds these names. Periods are calendar months in
`YYYY-MM` form unless stated. Currency is the reporting currency, INR in
the sample.

| Metric | Definition | Grain |
|---|---|---|
| `revenue` | Invoiced sales net of returns and credit notes, excluding tax. | Month |
| `billing_by_customer` | Invoiced amount per customer, same basis as revenue, top 10. | Month |
| `gross_margin_pct` | (Revenue minus cost of goods sold) divided by revenue, as a percentage. Cost of goods sold includes freight inward. | Month |
| `cash_balance` | Sum of all bank accounts at month end. | Month end |
| `receivables_overdue` | Invoices unpaid past their due date, total and count. | Month end |
| `headcount` | People on payroll on the last day of the month, full time and contract shown separately. | Month end |
| `orders` | Count of sales orders confirmed in the month. | Month |

## Derived figures the agent may compute

- Year to date for any monthly metric: the sum of months from April (start
  of the financial year) to the requested month. Show the months added.
- Change from the previous month: the difference and the percentage, shown
  with both months' values.

Anything else derived is out of scope.

## Notes

- Revenue is recognised on invoice date, not on dispatch or payment.
- Gross margin excludes warehouse wages and rent; those are operating
  expenses.
- `billing_by_customer` lists the top 10 only. Ask for a specific customer
  and the tool returns that customer if present in the month.
