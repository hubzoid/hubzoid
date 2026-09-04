---
tags: [canary]
expect_tools: [alerts_since, open_incidents]
timeout: 240
---
## Prompt
What fired overnight, and what is open at hand-over?

## Criteria
Reads the alerts and the open incidents and follows the hand-over format.
With the sample data: 5 alerts fired and 4 were suppressed; the suppressed
ones are reported with their rule (flap-damping twice, maintenance-window
once, dependency twice across web-3), never dropped. Open at hand-over are
INC-2211 (DiskFull on db-1, 9 hours, stale because the last update is
older than 4 hours) and INC-2212 (BackupFailed on backup-1). INC-2210 is
resolved and is not listed as open. Names INC-2211 as the first thing to do
with the first DiskFull runbook step. Rounding a count, dropping a
suppressed alert, or inventing a root cause is a failure.
