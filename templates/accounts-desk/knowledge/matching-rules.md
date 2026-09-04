---
name: matching-rules
description: Tolerances for bill-to-PO matching, the duplicate definition, and who decides each kind of exception.
keywords: [matching, tolerance, duplicate, escalation, three-way match]
---

# Matching rules (sample)

## Amount tolerance

A bill matches its PO when the bill total is within the smaller of 2 percent
or 500 of the PO total. Anything beyond that is an exception, however small
the reason looks.

## Quantity tolerance

Zero. A quantity difference of any size is an exception. Short deliveries are
matched against the goods receipt, not the PO, and still need a note.

## Duplicate definition

A bill is a suspected duplicate when either is true.

- Same supplier, same bill number, any date.
- Same supplier, same total, dated within 30 days of another bill.

A suspected duplicate is an exception even when the PO matches.

## Missing PO

A bill with no PO number is not matched. It goes back to the person who
ordered, through the accounts team, with the supplier and amount noted.

## Who decides

| Exception | Decides |
|---|---|
| Amount over tolerance, difference up to 25,000 | Accounts team lead |
| Amount over tolerance, difference above 25,000 | Finance head |
| Any bill above 1,000,000 | Finance head and managing director |
| Suspected duplicate | Accounts team lead, after checking the ledger |
| Supplier on hold | Finance head |
| Missing PO | The person who ordered, via the accounts team |

## Timing

Bills are checked the evening they arrive. Exceptions older than three
working days appear at the top of the next exceptions report.
