---
severity_floor: medium
ignore:
  - "docs/**"
  - "*.lock"
focus:
  - "every /orders and /admin route needs @login_required"
  - "database values go in as parameters, never inside the SQL text"
---

The storefront serves the Kestrel & Oak shop and the internal pages customer
care uses to look up orders. The orders table holds customer emails and
addresses, so a missing sign-in check on an /orders route is a data leak, not
a style problem.

The nightly supplier sync must finish: the warehouse reads its counts at
06:00. A sync that hangs is worse than one that fails and logs the error.
