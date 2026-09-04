---
name: handover-format
description: The structure of the morning digest, what counts as open at hand-over, and how suppressed alerts are reported.
keywords: [handover, digest, format, suppression, on-call]
---

# Hand-over digest format (sample)

The digest covers the window from the previous hand-over (19:00) to now.
Five sections, always in this order.

## 1. Counts

One line: alerts fired, alerts suppressed, incidents opened, incidents still
open. From the tools, exact.

## 2. Fired

One line per fired alert: time, alert name, host, severity, and whether an
incident was opened. Grouped by alert name when the same alert fired more
than three times on the same host; show the count and the first and last
time.

## 3. Suppressed

One line per suppression rule that acted, with the count of alerts it
suppressed and the hosts involved. Rules in use:

| Rule | What it does |
|---|---|
| maintenance-window | Alerts on hosts inside a declared maintenance window. |
| flap-damping | The same alert on the same host within 10 minutes of a resolve. |
| dependency | Alerts on hosts behind a parent that is already down. |

A suppressed alert is never dropped from the digest. The next person needs
to know the rule acted, so they can question it.

## 4. Open at hand-over

Every incident still open, one line: incident id, alert name, host, owner,
hours open, last update. An incident with no update in 4 hours is marked
"stale". Under each open incident whose alert name has a runbook, the first
two runbook steps.

## 5. First thing to do

One line. The open incident with the highest severity, then the oldest.
Name it and the first runbook step. If nothing is open, write "Nothing open
at hand-over" and stop.

## Style

Times in the server's local zone, 24-hour. Host names exact. No adjectives
that a count could replace.
