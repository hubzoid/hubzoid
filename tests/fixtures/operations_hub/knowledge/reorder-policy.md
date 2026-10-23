---
name: reorder-policy
description: How we decide what to reorder, how much, and who approves. The stock_check tool applies these rules to the stock export.
---

# Reorder policy

The `stock_check` tool applies this policy to the stock export. Report its
numbers. Do not recompute them.

## Rules

- **Safety stock:** 7 days of sales.
- **Reorder point:** (lead time + 7 safety days) x average daily sales. When
  stock on hand plus stock on order is below it, order this week.
- **Order quantity:** enough for the lead time, 28 days of sales after the
  delivery and the 7 safety days, less stock on hand and on order, rounded up
  to whole cases.
- **Stockout risk:** stock runs out before the next delivery can arrive (the
  open order, or an order placed today). Act today: expedite the open order,
  or order now and tell customer care which products will be missing.
- **Overstock:** more than 120 days of cover and nothing on order. Do not
  reorder. Tell buying.

## Placing orders

- One purchase order per supplier, sent on its order day (see `suppliers`).
- Each supplier has a minimum order. Below it, add the next products that are
  close to their reorder point, or wait for the next order day if nothing runs
  out first.
- A purchase order above EUR 2,000 needs the operations lead's approval before
  it is sent.
- The assistant drafts purchase orders and emails. A person sends them.
