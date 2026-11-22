"""What the workflow agent tools say (hubzoid.tools.workflow_tools): each
workflow line (schedule with timezone, state, next and last run, who it runs
as, an unreadable task file), run lists and one run in detail (output only
for the account it acted as, steps capped), durations and times; refusals
per workflow name; a service error is a short sentence, never the raw
exception; bad inputs, an already running workflow, a schedule already
paused or not paused. The service (workflows.control) is replaced by fakes,
as in tests/test_tools_workflow.py."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hubzoid.access import Identity, identity_scope
from hubzoid.tools import workflow_tools as wt
from hubzoid.workflows import access as wf_access
from hubzoid.workflows import control
from tests.test_access_service import dep  # noqa: F401
from tests.test_tools_workflow import ANN, _grant, _invoke, _tools

T0 = int(datetime(2026, 10, 2, 3, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_times_and_durations():
    ist = timezone(timedelta(hours=5, minutes=30))
    assert wt._clock(datetime(2026, 10, 2, 8, 30, tzinfo=ist)) == "2026-10-02 08:30 UTC+05:30"
    assert wt._clock(datetime(2026, 10, 2, 8, 30)) == "2026-10-02 08:30"
    assert wt._iso("not a time") == "not a time" and wt._ms(None) == "?"
    assert wt._ms(T0) == "2026-10-02 03:00 UTC"
    assert [wt._took(v) for v in (None, 4_500, 125_000, 7_380_000)] == ["", "4s", "2m 5s", "2h 3m"]
    assert wt._status("SUCCESS") == "succeeded" and wt._status("WEIRD") == "weird"
    assert wt._cap("x" * 10, 4) == "xxxx …(cut)" and wt._cap(None, 4) == ""


def test_schedules_in_words():
    assert wt._schedule({}) == "manual only"
    assert wt._schedule({"schedule": "on webhook orders"}) == "on webhook orders"
    assert wt._schedule({"schedule": "0 9 * * *", "timezone": "Asia/Kolkata"}).endswith("(Asia/Kolkata)")
    assert wt._schedule({"schedule": "daily at 6am"}) == "daily at 6am (UTC)"


def test_a_workflow_line():
    assert "can't be read" in wt._workflow_line({"name": "md:?"})
    line = wt._workflow_line({"name": "md:daily", "kind": "markdown", "schedule": "daily at 6am",
                              "state": "paused", "next_run": "2026-10-03T06:00:00+00:00",
                              "last_run": {"status": "ERROR", "created": T0},
                              "runs_as": {"account": "reports@x.org"}, "as_tool": True, "schedulable": True})
    assert line.startswith("- md:daily (scheduled task): daily at 6am (UTC). Paused; next run 2026-10-03 06:00 UTC.")
    assert "Last run failed, 2026-10-02 03:00 UTC." in line and "Runs as reports@x.org." in line
    assert line.endswith("people can run it for themselves and schedule it.")
    broken = wt._workflow_line({"name": "weekly", "state": "error", "runs_as": {"error": "no account"}})
    assert "(code workflow)" in broken and "No runs yet." in broken and "no usable account" in broken


def test_runs_and_one_run_in_detail():
    line = wt._run_line({"id": "r-1", "name": "weekly", "status": "ERROR", "started": T0, "duration_ms": 61_000,
                         "error": "boom " * 100})
    assert line.startswith("- r-1 · weekly · failed · started 2026-10-02 03:00 UTC · took 1m 1s\n  error: boom")
    assert line.endswith("…(cut)")
    steps = [{"name": f"s{i}", "completed": True} for i in range(wt.MAX_ROWS + 2)]
    steps[0] = {"name": "fetch", "error": "timeout"}
    steps[1] = {"name": "write"}
    detail = wt._run_detail({"id": "r-1", "name": "weekly", "status": "SUCCESS", "started": T0, "completed": T0 + 5000,
                             "duration_ms": 5000, "run_as": "reports@x.org", "output": "42 rows", "steps": steps})
    assert "finished 2026-10-02 03:00 UTC (took 5s)." in detail and "It acted as reports@x.org." in detail
    assert "Output: 42 rows" in detail and "1. fetch · failed: timeout" in detail and "2. write · running" in detail
    assert detail.endswith("…and 2 more steps.")
    hidden = wt._run_detail({"id": "r-2", "name": "weekly", "status": "PENDING", "started": T0, "redacted": True})
    assert "only the account the run acted as can see it" in hidden and "Started 2026-10-02 03:00 UTC." in hidden


@pytest.fixture
def tools(dep, monkeypatch):  # noqa: F811
    _grant(dep, "workflows_view", "workflows_manage")
    monkeypatch.setattr(wf_access, "canonical", lambda hub_dir, name: name)
    allowed = {"daily"}
    monkeypatch.setattr(wf_access, "authorize", lambda hub_dir, who, key, action: type(
        "D", (), {"allowed": key in allowed})())
    with identity_scope(Identity.make(ANN, surface="owui")):
        yield _tools(dep, monkeypatch)


def fail_with(monkeypatch, name, exc):
    def boom(*a, **kw):
        raise exc

    monkeypatch.setattr(control, name, boom)


def test_listing_hides_what_you_cant_see(tools, monkeypatch):
    monkeypatch.setattr(control, "overview", lambda hub_dir, viewer: [{"name": "secret_payroll", "state": "manual"}])
    assert _invoke(tools["list_workflows"], {}) == "This agent has no workflows or scheduled tasks you can see."
    rows = [{"name": "daily", "state": "scheduled", "schedule": "daily at 6am"}] * (wt.MAX_ROWS + 1)
    monkeypatch.setattr(control, "overview", lambda hub_dir, viewer: rows)
    out = _invoke(tools["list_workflows"], {})
    assert out.startswith(f"{wt.MAX_ROWS + 1} workflow(s)") and out.endswith("…and 1 more.")


def test_runs_listing_detail_and_refusals(tools, monkeypatch):
    args = {"workflow": "", "run_id": "", "status": "", "limit": 10}
    assert "You can't see the runs of secret_payroll" in _invoke(tools["workflow_runs"], {**args, "workflow": "secret_payroll"})
    run = {"id": "r-1", "name": "daily", "workflow": "daily", "status": "SUCCESS", "started": T0}
    monkeypatch.setattr(control, "history", lambda hub_dir, **kw: [run, {**run, "workflow": "secret_payroll"}])
    out = _invoke(tools["workflow_runs"], args)
    assert out.splitlines() == ["Runs, newest first:", "- r-1 · daily · succeeded · started 2026-10-02 03:00 UTC"]
    assert _invoke(tools["workflow_runs"], {**args, "run_id": "r-1"}).startswith("Run r-1 of daily: succeeded.")
    monkeypatch.setattr(control, "history", lambda hub_dir, **kw: [])
    assert _invoke(tools["workflow_runs"], {**args, "run_id": "r-9"}) == "There is no run 'r-9' in this agent."
    assert _invoke(tools["workflow_runs"], args) == "No runs found."
    fail_with(monkeypatch, "history", control.ControlError("Use running, failed, succeeded or cancelled.", "bad_filter"))
    assert _invoke(tools["workflow_runs"], {**args, "status": "odd"}) == \
        "[not available: Use running, failed, succeeded or cancelled.]"


@pytest.mark.parametrize("tool,args,service", [
    ("list_workflows", {}, "overview"),
    ("workflow_runs", {"workflow": "", "run_id": "", "status": "", "limit": 10}, "history"),
    ("pause_workflow", {"name": "daily"}, "set_paused"),
    ("resume_workflow", {"name": "daily"}, "set_paused"),
    ("cancel_workflow_run", {"run_id": "r-1"}, "cancel"),
])
def test_a_service_error_is_never_shown_raw(tools, monkeypatch, tool, args, service):
    monkeypatch.setattr(control, "history", lambda hub_dir, **kw: [])
    fail_with(monkeypatch, service, RuntimeError("psycopg: password authentication failed for user hub"))
    out = _invoke(tools[tool], args)
    assert out == f"[not available: {wt._UNAVAILABLE}]" and "psycopg" not in out


def test_starting_a_run(tools, monkeypatch):
    run = tools["run_workflow"]
    assert "inputs must be a JSON object" in _invoke(run, {"name": "daily", "inputs": "{oops"})
    assert "inputs must be a JSON object" in _invoke(run, {"name": "daily", "inputs": "[1, 2]"})
    assert "You can't run secret_payroll" in _invoke(run, {"name": "secret_payroll"})
    monkeypatch.setattr(control, "start_now", lambda *a, **kw: {"workflow": "daily", "run_id": "r-7",
                                                                "already_running": True, "runs_as": None})
    assert _invoke(run, {"name": "daily", "inputs": '{"program": "Advanced"}'}) == \
        "daily is already queued or running (run id r-7), so no new run was started."
    monkeypatch.setattr(control, "start_now", lambda *a, **kw: {"workflow": "daily", "run_id": "r-8",
                                                                "already_running": False, "runs_as": None})
    assert "It runs as the workflow's own account, not as you" in _invoke(run, {"name": "daily"})
    fail_with(monkeypatch, "start_now", control.ControlError("Workflows are switched off.", "code_off"))
    assert _invoke(run, {"name": "daily"}) == "[not started: Workflows are switched off.]"
    fail_with(monkeypatch, "start_now", RuntimeError("dbos down"))
    assert "could not be started right now" in _invoke(run, {"name": "daily"})


def test_pausing_resuming_and_cancelling(tools, monkeypatch):
    unchanged = {"workflow": "daily", "changed": False}
    monkeypatch.setattr(control, "set_paused", lambda *a, **kw: unchanged)
    assert _invoke(tools["pause_workflow"], {"name": "daily"}) == "daily was already paused."
    assert _invoke(tools["resume_workflow"], {"name": "daily"}) == "daily was not paused. Its schedule is unchanged."
    monkeypatch.setattr(control, "set_paused", lambda *a, **kw: {**unchanged, "changed": True})
    assert _invoke(tools["resume_workflow"], {"name": "daily"}) == "Resumed daily. Its scheduled runs start again."
    for name in ("pause_workflow", "resume_workflow"):
        assert "You can't pause, resume or cancel secret_payroll" in _invoke(tools[name], {"name": "secret_payroll"})
    fail_with(monkeypatch, "set_paused", control.ControlError("No workflow called nope.", "unknown"))
    assert _invoke(tools["resume_workflow"], {"name": "daily"}) == "[not available: No workflow called nope.]"

    monkeypatch.setattr(control, "history", lambda hub_dir, **kw: [{"workflow": "secret_payroll"}])
    assert "You can't pause, resume or cancel secret_payroll" in _invoke(tools["cancel_workflow_run"], {"run_id": "r-1"})
    fail_with(monkeypatch, "history", RuntimeError("history down"))      # cancel reports a missing run itself
    fail_with(monkeypatch, "cancel", control.ControlError("That run already finished.", "finished"))
    assert _invoke(tools["cancel_workflow_run"], {"run_id": "r-1"}) == "[not available: That run already finished.]"
