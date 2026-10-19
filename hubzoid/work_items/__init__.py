"""Durable work items and safe external writes.

A work item is one piece of requested work ("triage issue 42", "reply to this
email") with a stable key, a route of stages it may run, and a state that
survives restarts, redrives and upgrades. A consequential external write goes
through `hub.operation.run`, which records the exact intent before anything is
sent and never repeats an uncertain send on its own.

The workflow engine (DBOS) does the durable execution: workflow ids, saved
steps, queues, versions and forks. This package keeps only what it does not:
the item and operation records, write modes and the decisions people make.

    from hubzoid import hub, workflow
    from hubzoid.work_items import Route

    @workflow(work_items={"answer": Route(stages=("plan",)),
                          "act": Route(stages=("plan", "send"), actions=("email.send",))})
    def desk():
        plan = hub.work_item.checkpoint("plan", lambda: make_plan(hub.work_item.input))
        if hub.work_item.allows("send"):
            hub.operation.run("send_reply", target={"thread": plan["thread"]},
                              arguments={"body": plan["body"]}, recovery="escalate",
                              actions="email.send", dispatch=send_reply, draft=save_draft)
        return plan
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import NotSent, Parked, Rejected, WorkItemError
from .operations import APPLIED, SAFE_TO_RETRY, UNKNOWN, OperationFailed, Reconciled


@dataclass(frozen=True)
class Route:
    """The stages a work item may run and the consequential actions it may
    take. `None` means no limit for that part."""

    stages: tuple | None = None
    actions: tuple | None = None

    def to_dict(self) -> dict:
        return {"stages": list(self.stages) if self.stages is not None else None,
                "actions": list(self.actions) if self.actions is not None else None}


def normalize_routes(value) -> dict:
    """`work_items=` from `@workflow`: True, a list of route names, or a
    mapping of route name to `Route` (or `{stages, actions}`)."""
    import re

    if value is True:
        return {"default": Route().to_dict()}
    if isinstance(value, (list, tuple)):
        value = {name: Route() for name in value}
    if not isinstance(value, dict) or not value:
        raise ValueError("work_items must be True, route names, or a mapping of routes")
    out = {}
    for name, route in value.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ValueError(f"Route names are lowercase identifiers, not {name!r}")
        if route is None:
            route = Route()
        if isinstance(route, dict):
            unknown = set(route) - {"stages", "actions"}
            if unknown:
                raise ValueError(f"Route {name!r} has unknown keys: {', '.join(sorted(unknown))}")
            route = Route(stages=tuple(route["stages"]) if route.get("stages") is not None else None,
                          actions=tuple(route["actions"]) if route.get("actions") is not None else None)
        if not isinstance(route, Route):
            raise ValueError(f"Route {name!r} must be a Route")
        out[name] = route.to_dict()
    return out


__all__ = ["Route", "WorkItemError", "Parked", "NotSent", "Rejected", "Reconciled",
           "OperationFailed", "APPLIED", "SAFE_TO_RETRY", "UNKNOWN", "normalize_routes"]
