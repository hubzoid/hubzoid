---
name: reconciliation-rules
description: What counts as a match between a supplier slip and a goods receipt entry, tolerances per material class, and the mismatch categories.
keywords: [reconciliation, slip, goods receipt, tolerance, mismatch]
---

# Reconciliation rules (sample)

## The matching key

A slip and a ledger entry belong together when the slip number matches. The
supplier name is checked second, the material third. Quantity is never used
to pair records; it is only compared once they are paired.

## Tolerances by material class

| Material class | Examples | Tolerance |
|---|---|---|
| Counted items | Boxes, cartons, fastener packs | Zero. Any difference is a mismatch. |
| Weighed bulk | Wire coil, steel bar, resin | 0.5 percent of slip quantity or 5 kg, whichever is larger. |
| Measured length | Stretch film, strapping | 1 percent of slip quantity. |

Units must agree. A slip in kg against a ledger entry in pieces is a
mismatch of type "wrong supplier or material" until a person resolves it.

## Date window

A ledger entry dated the same day as the slip, or the next day, matches. A
slip with no ledger entry within that window is "missing in ledger".

## Mismatch categories

| Category | Definition |
|---|---|
| Quantity mismatch | Paired, but quantity outside tolerance. |
| Missing in ledger | Slip exists, no ledger entry with that slip number in the window. |
| Missing slip | Ledger entry exists, no slip with that number. |
| Wrong supplier or material | Paired by slip number, but supplier, material, or unit disagree. |

## What the report contains

One line per mismatch: slip number, supplier, category, the exact difference
with units, and the owner from the escalation rules. Matched slips are
counted, not listed.
