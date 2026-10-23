---
name: supplier-delay
description: Work out what a late supplier delivery puts at risk, the options to close the gap, and messages for customers and the supplier.
---

# Supplier delay

Use this when a supplier says a delivery will be late.

1. Call `stock_check(supplier=..., delay_days=...)` with the days of delay.
2. List the products that now run out before their delivery: days without
   stock and estimated lost sales. Also name products that still arrive in
   time but with fewer than 3 `days_to_spare`, and products of that supplier
   already at risk for another reason.
3. Check `suppliers` for expediting options and their cost, and
   `reorder-policy` for approvals. Propose the cheapest option that closes the
   gap. Show how you worked out any cost.
4. Draft a short message for customers waiting on those products, following
   `customer-messages`.
5. Draft a two-line note to the supplier asking for the new date in writing.

Do not send anything. Every stock number comes from `stock_check`.
