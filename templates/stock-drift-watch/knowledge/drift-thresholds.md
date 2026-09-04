---
name: drift-thresholds
description: What size of difference between system and counted stock is worth acting on, and how to rank locations for a cycle count.
keywords: [drift, threshold, cycle count, variance, recount]
---

# Drift thresholds (sample)

Drift is the difference between the system quantity and the last counted
quantity for one SKU at one location. Value of drift is that difference
times unit cost.

## Action by drift size

| Drift | Action |
|---|---|
| Below 2 percent of system quantity and below 5,000 in value | Note only. No action. |
| 2 to 5 percent, or 5,000 to 25,000 in value | Recount within 7 days by the location lead. |
| Above 5 percent, or above 25,000 in value | Recount within 2 days, and the inventory controller reviews the movement history. |
| Any drift on a controlled item (class A) | Recount within 2 days, whatever the size. |

The stricter row wins when two apply.

## Ranking locations for a cycle count

Rank by total absolute drift value across all SKUs at the location. Ties go
to the location with the older last count. Name the top location as the one
to count first.

## Item classes

| Class | Definition | Examples |
|---|---|---|
| A | Top 20 percent of SKUs by annual value, or any item above 2,000 unit cost | Stainless fastener kits, bearings |
| B | The next 30 percent | Standard MS fasteners |
| C | Everything else | Packaging, consumables |

## Stale counts

A SKU not counted in 90 days is listed with "count overdue" regardless of
drift, because the drift figure means little against a stale count.
