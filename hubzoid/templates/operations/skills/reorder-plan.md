---
name: reorder-plan
description: Build today's reorder plan from the stock export, one purchase order per supplier, with approvals and expedites flagged.
---

# Reorder plan

Use this when someone asks what to reorder, what could run out, or for a
purchase order draft.

1. Call `stock_check()` with no filters.
2. Start with what needs action today: the `out_of_stock` and `stockout_risk`
   rows. For each, give the action (order now, or expedite the open order) and
   how many days customers would be without it
   (`stockout_days_before_next_delivery`).
3. Then the `reorder` rows: order this week.
4. Use `order_by_supplier` for one purchase order per supplier. Check each
   total against the supplier's minimum order and order day in `suppliers`,
   and mark any order above EUR 2,000 for the operations lead's approval
   (`reorder-policy`).
5. Mention `overstock` rows in one line, for buying.
6. End with the first three things to do today, in order, each with an owner
   from `company`.

Answer with tables: product, SKU, status, days of cover, suggested quantity
and order value. Every number comes from `stock_check`.
