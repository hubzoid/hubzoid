---
name: chart-of-accounts-conventions
description: Account codes and the shape of a draft purchase entry. Sample values, replace with your own chart.
keywords: [chart of accounts, account codes, entry, journal, payables]
---

# Chart of accounts conventions (sample)

Replace these codes with your own chart before going live. The agent uses
this file to draft entries, so an unlisted code is never used.

## Expense and asset codes by purchase type

| Purchase type | Debit account | Code |
|---|---|---|
| Packaging materials | Packaging consumables | 5100 |
| Freight and transport inward | Freight inward | 5200 |
| Fasteners and stock for resale | Inventory, trading stock | 1300 |
| Office supplies and stationery | Office expenses | 6300 |
| Software subscriptions | IT subscriptions | 6410 |
| Repairs and maintenance | Repairs | 6520 |

## Other codes every entry needs

| Purpose | Account | Code |
|---|---|---|
| Supplier balance | Trade payables | 2100 |
| Tax input credit | Input tax recoverable | 1450 |

## Shape of a draft entry

```
DRAFT · Bill INV-2288 · Kestrel Logistics · PO-1190
Dr 5200 Freight inward            10,508
Dr 1450 Input tax recoverable      1,892
    Cr 2100 Trade payables (Kestrel Logistics)   12,400
Narration: Freight inward, PO-1190, bill INV-2288 dated 2026-08-28
```

Tax is split out at the rate shown on the bill. The credit to trade payables
is always the bill total. Narration carries bill number, PO number, and bill
date in that order.

## Cost centres

| Location | Cost centre |
|---|---|
| Pune | PN |
| Ahmedabad | AH |
| Coimbatore | CB |

Append the cost centre of the receiving location to each debit line when the
PO names one.
