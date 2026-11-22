"""One turn on the AgentCore runner (hubzoid.runner.server), in process with
a fake runtime: its output streams in order and ends with exactly one end
event; a failure while building or during the turn, or the runtime's own last
error, is reported in that end event and recorded as the call's outcome;
a scheduled task that isn't in the runner's hub is refused by name; a call
the hub service stopped renewing is cancelled; a cancelled turn's budget hold
is settled, and a failed settle never breaks the stop."""
from __future__ import annotations

import asyncio

from sqlalchemy import text
from starlette.testclient import TestClient

from hubzoid import runtime as runtime_lib
from hubzoid.runner import calls, protocol, server
from tests.test_agentcore_runner import hub  # noqa: F401
from tests.test_cov_runner_server import SESSION, events


class Runtime:
    """A runtime double: ``stream`` yields text (as plug-in runtimes do)."""

    def __init__(self, parts=("Hello", " Ana"), fail=None, last_error=None):
        self.parts, self.fail, self.last_error, self.opened, self.closed = parts, fail, last_error, 0, 0

    async def aopen(self):
        self.opened += 1

    async def aclose(self):
        self.closed += 1

    async def stream(self, prompt):
        for part in self.parts:
            yield part
        if self.fail:
            raise self.fail


def turn(hub_dir, call_id, rt=None, *, monkeypatch, options=None, build_fails=None):
    def build(hub_dir, **kw):
        if build_fails:
            raise build_fails
        return rt

    monkeypatch.setattr(runtime_lib, "_build", build)
    body = {"v": 1, "kind": "turn", "call": call_id, "release": protocol.release(), "prompt": "hi",
            "options": options or {}, "context": {}}
    calls.create(hub_dir, call_id=call_id, hub="h", kind="turn", session_id=next(iter(SESSION.values())),
                 request_digest=protocol.digest(body), identity={})
    with TestClient(server.create_app(hub_dir)) as client:
        got = events(client.post("/invocations", json=body, headers=SESSION).text)
    return got


def outcome(hub_dir, call_id):
    with calls._engine(hub_dir).connect() as c:
        return tuple(c.execute(text("SELECT state, outcome, error FROM hz_runner_calls WHERE id=:id"),
                               {"id": call_id}).first())


def test_a_turn_streams_its_output_and_ends_once(hub, monkeypatch):  # noqa: F811
    rt = Runtime()
    got = turn(hub, "rc_ok", rt, monkeypatch=monkeypatch)
    assert [e["type"] for e in got] == ["start", "text", "text", "end"]
    assert "".join(e["text"] for e in got if e["type"] == "text") == "Hello Ana"
    assert [e["seq"] for e in got] == [1, 2, 3, 4] and got[-1]["error"] is None and got[-1]["cancelled"] is False
    assert rt.opened == rt.closed == 1 and outcome(hub, "rc_ok") == ("ended", "ok", None)


def test_failures_end_the_turn_with_the_reason(hub, monkeypatch):  # noqa: F811
    got = turn(hub, "rc_mid", Runtime(fail=ConnectionError("model went away")), monkeypatch=monkeypatch)
    assert got[-1]["type"] == "end" and got[-1]["error"] == "ConnectionError: model went away"
    assert "".join(e.get("text", "") for e in got) == "Hello Ana"          # what streamed before stays
    assert outcome(hub, "rc_mid") == ("ended", "error", "ConnectionError: model went away")

    got = turn(hub, "rc_build", monkeypatch=monkeypatch, build_fails=ValueError("no model configured"))
    assert [e["type"] for e in got] == ["start", "end"] and got[-1]["error"] == "ValueError: no model configured"

    got = turn(hub, "rc_last", Runtime(last_error=PermissionError("tool refused")), monkeypatch=monkeypatch)
    assert got[-1]["error"] == "PermissionError: tool refused" and outcome(hub, "rc_last")[1] == "error"


def test_a_scheduled_task_must_be_in_the_runners_hub(hub, monkeypatch):  # noqa: F811
    got = turn(hub, "rc_sched", Runtime(), monkeypatch=monkeypatch,
               options={"bundle": {"kind": "schedule", "task": "weekly_sales"}})
    assert got[-1]["error"] == "LookupError: schedule/weekly_sales.md is not in this runner's hub"


def test_a_call_the_hub_stopped_renewing_is_cancelled(hub, monkeypatch):  # noqa: F811
    monkeypatch.setattr(server, "CANCEL_POLL_SECONDS", 0)
    answers = iter([RuntimeError("database busy"), True, False])

    def running(hub_dir, call_id):
        value = next(answers)
        if isinstance(value, Exception):
            raise value                                                # unknown: keep going
        return value

    monkeypatch.setattr(calls, "running", running)

    async def scenario():
        worker = asyncio.create_task(asyncio.sleep(30))
        await server._watch_cancel(hub, "rc_watch", worker)
        await asyncio.sleep(0)
        return worker.cancelled()

    assert asyncio.run(scenario()) is True


def test_a_cancelled_turns_budget_hold_is_settled(hub, monkeypatch):  # noqa: F811
    from hubzoid import budgets

    settled = []
    monkeypatch.setattr(budgets, "settle_in", lambda conn, reservation: settled.append(reservation))
    server._settle_hold(hub, {})
    server._settle_hold(hub, {"usage": {"budget_reservation": "hold-1"}})
    assert settled == ["hold-1"]

    def broken(conn, reservation):
        raise RuntimeError("budgets table locked")

    monkeypatch.setattr(budgets, "settle_in", broken)
    server._settle_hold(hub, {"usage": {"budget_reservation": "hold-2"}})   # logged, never raised
    assert settled == ["hold-1"]
