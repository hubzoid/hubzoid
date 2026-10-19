# Repeated requests: "repeat this every Monday" as a personal workflow

## Problem
People ask the agent the same thing every week. Personal schedules today
only repeat a report someone wrote as code, so "every Monday at 9, list the
low stock" has nowhere to go.

## Why now
Workflows are the one way Hubzoid runs work on a timetable. A person's own
repeated question should use the same path, with the same access checks,
pause and stop, instead of a second scheduler.

## What should happen
- Hubzoid ships one workflow, `repeated_request`. Its input is the exact
  request. Each run asks the hub's agent that request fresh, as the person,
  and puts the answer in a new private chat for them.
- In chat, "repeat this every Monday at 9" calls `repeat_request`: it shows
  the exact saved request, the next runs and where answers arrive, and saves
  only after a yes in the person's next message (the same two-step
  confirmation as other personal schedules). A first run starts on save.
- Who may save one is workflow access: Run and Schedule on
  `repeated_request`. Nobody has it until an admin gives it.
- It appears under Settings, Schedules with the person's other schedules:
  Run now, Pause, Resume, Edit and Stop work as before. Its last result
  reads "Answered in a new chat".
- Unattended runs never wait for a person: a tool that needs approval is
  refused and the answer says so.
- `HUBZOID_PROMPT_WORKFLOWS=false` turns the built-in off.

## Out of scope
Team-wide repeated requests, delivery to Slack or WhatsApp, and email of the
answer.

## Compatibility
Additive. No new tables. A hub's own workflow named `repeated_request`
would clash; none exist today.
