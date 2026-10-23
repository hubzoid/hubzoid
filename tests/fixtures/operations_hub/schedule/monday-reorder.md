---
# Every Monday at 08:00, write the week's reorder plan to output/reorder/.
# Off until you set `enabled: true`. Docs:
# https://github.com/hubzoid/hubzoid/blob/main/docs/schedule.md
schedule: "0 8 * * 1"
enabled: false
write: ["output/reorder"]
---

Build this week's reorder plan with the reorder-plan skill. Save it with
write_hub_file as output/reorder/<today's date as YYYY-MM-DD>.md for the
operations lead to review: purchase orders by supplier, approvals needed and
products to expedite. You are done when the file is written.
