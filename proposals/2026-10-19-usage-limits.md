# Usage limits per run, work item and month

Status: implementation authorized by the owner on 18 October 2026, for 2.0.

## Problem

A hub can run unattended work, isolated agents and many chat replies. Today it
records what each call used (`hz_usage`), but nothing stops a run, a piece of
work or a month from using more than its owner intended.

## What should happen

Keep it thin and opt-in. With no file, nothing changes.

1. **One optional file**, `budgets.yaml`: limits in tokens, turns or dollars
   for each run, each work item and the hub per UTC month. Validated with file
   and line; settable by file, `hubzoid budgets` or the Console.
2. **One check before every model call.** Usage already recorded, plus what
   running calls hold, is compared with each applicable limit under a lock. A
   used-up scope refuses the call before anything is charged, with a plain
   message.
3. **The runtimes' own caps as the backstop** inside a run: Claude's
   `max_turns` and `max_budget_usd`, the turn limit on Codex and the OpenAI
   Agents runtime, set from what is left. A reply stopped there says so, and
   "continue" starts a new, checked run.
4. **Prices from what exists**: Claude's reported cost, then LiteLLM's price
   table. A dollar limit refuses a model with no price instead of running
   unbounded. Token and turn limits work with subscription logins.
5. **Honest about the unknown**: a call that never reported keeps holding its
   amount until an administrator releases it, with a reason.

## Scope and non-goals

- No per-person, per-account or per-agent allocations, no price tables of our
  own, no context compaction settings.
- Token limits stop the next call, not one already running.

## Definition of done

- No file means no limits. A used-up scope refuses before any provider call.
- Runs at the same time with a run limit cannot together pass a limit.
- Claude and Codex runs stop at their caps, and the person can continue.
- A dollar limit never runs an unpriced model.
