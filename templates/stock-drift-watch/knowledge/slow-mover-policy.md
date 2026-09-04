---
name: slow-mover-policy
description: Days-without-movement bands, what to do in each band, and who decides on transfers and write-downs.
keywords: [slow mover, dead stock, movement, ageing, transfer]
---

# Slow-mover policy (sample)

A slow-mover is a SKU with quantity on hand and no issue or sale from that
location for longer than the band threshold. Receipts do not count as
movement.

## Bands

| Days without movement | Band | Action |
|---|---|---|
| 60 to 120 | Watch | Listed. Location lead confirms the stock is real and saleable. |
| 121 to 240 | Move | Inventory controller checks whether another location is consuming the SKU and proposes a transfer. |
| Above 240 | Decide | Listed for the finance head with value on hand. Candidates for markdown or write-down. The agent never decides this. |

## Exclusions

- Safety stock items flagged in the system are never slow-movers.
- Seasonal items are excluded from June to August. Replace with your own
  seasons.

## Value at stake

Value on hand is quantity times unit cost from the snapshot. Report the
total per band and per location so the operations head can see the money
in one glance.

## Who decides

| Action | Decides |
|---|---|
| Transfer between locations | Inventory controller |
| Markdown or write-down | Finance head |
| Stopping a reorder | Procurement lead |
