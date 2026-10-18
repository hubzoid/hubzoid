"""`hub.work_item` and `hub.operation`, inside a running workflow.

Stages are DBOS steps named `hz_stage:<stage>`: a finished stage returns its
saved output on recovery, redrive and continuation, so its model calls are
never paid for twice. Follow-up messages and counters are read through steps
too, so a replay sees the same values.
"""
from __future__ import annotations

from typing import Any, Callable

from . import engine as engine_lib
from . import operations as ops_lib
from . import store
from .errors import WorkItemError


def _ctx():
    from ..workflows import context

    return context._ctx()


def _work():
    ctx = _ctx()
    work = ctx.work or {}
    if not work.get("item"):
        raise WorkItemError("not_a_work_item",
                            "This run is not a work item. Declare the workflow with "
                            "@workflow(work_items=True) and start it as a work item.")
    return ctx, work


def _in_engine() -> bool:
    try:
        from dbos._context import get_local_dbos_context
    except ImportError:  # pragma: no cover
        return False
    ctx = get_local_dbos_context()
    return bool(ctx is not None and ctx.workflow_id)


def _step(name: str, fn: Callable[[], Any]) -> Any:
    if _in_engine():
        from dbos import DBOS

        return DBOS.run_step({"name": name}, fn)
    return fn()


def _jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


class WorkItem:
    """The work item this run is executing."""

    @property
    def id(self) -> str:
        return _work()[1]["item"]["id"]

    @property
    def generation(self) -> int:
        return _work()[1]["item"]["generation"]

    @property
    def route(self) -> str:
        return _work()[1]["item"]["route"]

    @property
    def requester(self) -> str:
        return _work()[1]["item"].get("requester") or ""

    @property
    def input(self) -> Any:
        return (_work()[1]["item"].get("input_json") or {}).get("input")

    def allows(self, stage: str) -> bool:
        stages = (_work()[1]["item"].get("route_json") or {}).get("stages")
        return stages is None or stage in stages

    def checkpoint(self, stage: str, run: Callable[[], Any], *, response_model=None) -> Any:
        """Run one stage once. Its output is saved; a recovered, redriven or
        continued run gets the saved output instead of running it again.

        `response_model` (a Pydantic model) validates the output and is
        returned on replay too. A stage outside this item's route is refused
        before it runs."""
        ctx, work = _work()
        if not isinstance(stage, str) or not stage or len(stage) > 100:
            raise ValueError("A stage name is 1 to 100 characters.")
        if not self.allows(stage):
            raise WorkItemError(
                "scope_exceeded",
                f"Route {work['item']['route']!r} does not include stage {stage!r}. A wider route "
                "needs a new request from someone allowed to ask for it.", status=403)

        def body():
            previous = work.get("stage")
            work["stage"] = stage
            try:
                value = run()
            finally:
                work["stage"] = previous
            if response_model is not None:
                value = value if isinstance(value, response_model) else response_model.model_validate(value)
            return _jsonable(value)

        saved = _step(engine_lib.STAGE_PREFIX + stage, body)
        return response_model.model_validate(saved) if response_model is not None else saved

    def narrow(self, route: str) -> None:
        """Move this work to a narrower route of the same workflow (fewer
        stages and actions). Widening is never allowed from inside a run."""
        ctx, work = _work()
        from ..workflows.runtime import definition

        wf = definition(ctx.hub_dir, work["item"]["workflow"])
        routes = (wf.extra or {}).get("work_items") or {}
        if route not in routes:
            raise WorkItemError("route_missing", f"There is no route {route!r}.", status=422)
        old, new = work["item"].get("route_json") or {}, routes[route]
        for key in ("stages", "actions"):
            if new.get(key) is None and old.get(key) is not None:
                raise WorkItemError("scope_exceeded", "That route is wider than this work's route.", status=403)
            if new.get(key) is not None and old.get(key) is not None and not set(new[key]) <= set(old[key]):
                raise WorkItemError("scope_exceeded", "That route is wider than this work's route.", status=403)

        def body():
            item = store.get_item(ctx.hub_dir, work["item"]["id"])
            return store.update_item(ctx.hub_dir, item["id"], expected_revision=item["revision"],
                                     route=route, route_json=new)["revision"]

        _step("hz_narrow:" + route, body)
        work["item"] = dict(work["item"], route=route, route_json=new)

    def messages(self) -> list[dict]:
        """Follow-ups people sent since the last call: `{id, from, text, at}`.
        Read at a stage boundary. Each message is returned once."""
        ctx, work = _work()

        def body():
            item = store.get_item(ctx.hub_dir, work["item"]["id"])
            inbox = list(item.get("inbox_json") or [])
            fresh = [m for m in inbox if not m.get("consumed")]
            if not fresh:
                return []
            for m in inbox:
                m["consumed"] = True
            store.update_item(ctx.hub_dir, item["id"], expected_revision=item["revision"], inbox_json=inbox)
            return [{k: m.get(k) for k in ("id", "from", "text", "at")} for m in fresh]

        return _step("hz_inbox", body)

    def reserve(self, counter: str, key: str, limit: int) -> int | None:
        """Count something once per `key`, up to `limit` (a review round, a
        retry). Returns this key's number, or None when the limit is reached.
        Replays and races count it once."""
        ctx, work = _work()

        def body():
            for _ in range(5):
                item = store.get_item(ctx.hub_dir, work["item"]["id"])
                counters = dict(item.get("counters_json") or {})
                entry = dict(counters.get(counter) or {"keys": {}})
                keys = dict(entry["keys"])
                if key in keys:
                    return keys[key]
                if len(keys) >= limit:
                    return None
                keys[key] = len(keys) + 1
                counters[counter] = {"keys": keys}
                try:
                    store.update_item(ctx.hub_dir, item["id"], expected_revision=item["revision"],
                                      counters_json=counters)
                    return keys[key]
                except WorkItemError:
                    continue
            raise WorkItemError("revision_conflict", "Could not record the counter; try again.", status=409)

        return _step(f"hz_reserve:{counter}:{key}", body)


class Operation:
    """`hub.operation.run(...)`: see `hubzoid.work_items.operations.run`."""

    @staticmethod
    def run(name: str, **kwargs) -> Any:
        return ops_lib.run(name=name, **kwargs)
