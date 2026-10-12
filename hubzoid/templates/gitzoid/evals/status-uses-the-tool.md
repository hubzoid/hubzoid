---
expect_tools: [gitzoid_status]
tags: [gitzoid]
timeout: 180
---
## Prompt
What is waiting or stuck right now?

## Criteria
Uses gitzoid_status. Lists pull requests by state with their numbers, names
any with blocking findings, and mentions open security items. Does not invent
pull requests.
