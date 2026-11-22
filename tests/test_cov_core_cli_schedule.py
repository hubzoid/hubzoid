"""Schedule CLI history, named refusals and workflow dispatch through injected seams."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from hubzoid import cli, scheduling
from hubzoid.workflows import client, context, control, observe, runtime
from tests.test_schedule import _task
from tests.test_schedule_controls import hub  # noqa: F401


def _invoke(*args):
    return CliRunner().invoke(cli.app, ["schedule", *map(str, args)])


def _workflow(**values):
    return {"name": "nightly", "state": "paused", "timezone": "UTC", "next_run": None,
            "missed": 2, "error": "code unavailable", "runs_as": {"error": None,
                "account": "service@example.org", "source": "legacy-service", "via": "workflow"}, **values}


def test_schedule_list_renders_workflow_identity_errors_and_task_flags(hub, monkeypatch):
    tasks = [_task("script", run=["echo", "hello"], model="codex-local", commit=["output"], push=True),
             _task("disabled", enabled=False), _task("webhook", on_webhook="alert")]
    monkeypatch.setattr(scheduling, "load_tasks", lambda path: (tasks, ["bad task file"]))
    monkeypatch.setattr(observe, "catalog", lambda path: [_workflow(),
        _workflow(name="invalid", runs_as={"error": "account missing"})])
    result = _invoke("list", hub)
    assert result.exit_code == 1
    for text in ("legacy service identity", "cannot run: account missing", "code unavailable",
                 "script; model: codex-local; commit: output + push", "disabled", "on webhook 'alert'", "bad task file"):
        assert text in result.output
    result = _invoke("list", hub, "--json")
    body = json.loads(result.stdout)
    assert result.exit_code == 1 and body["problems"] == ["bad task file"]
    assert body["tasks"][0]["script"] is True
    assert body["tasks"][1]["next_fire"] is None and body["tasks"][2]["on_webhook"] == "alert"


def test_schedule_history_failure_is_explicit_in_json_and_terminal(hub, monkeypatch):
    monkeypatch.setattr(observe, "catalog", lambda path: [_workflow()])
    monkeypatch.setattr(observe, "runs", Mock(side_effect=OSError("history offline")))
    state = scheduling.ScheduleState(hub)
    state.record_fired("daily", datetime(2026, 11, 22, 3), result="error", run_log="run.jsonl")
    result = _invoke("status", hub, "--json")
    body = json.loads(result.stdout)
    assert result.exit_code == 0 and body["runs"] is None and body["history_error"] == "history offline"
    result = _invoke("status", hub)
    assert result.exit_code == 0
    assert "Workflow history unavailable: history offline" in result.output
    assert "last_result: error" in result.output and "last_run_log: run.jsonl" in result.output


def test_schedule_history_renders_run_failure_and_unseen_task(hub, monkeypatch):
    monkeypatch.setattr(observe, "catalog", lambda path: [])
    monkeypatch.setattr(observe, "runs", lambda *a, **kw: [{"id": "run-1", "name": "nightly",
        "status": "ERROR", "duration_ms": 12, "error": "tool refused"}])
    result = _invoke("status", hub)
    assert result.exit_code == 0 and "run-1 · nightly · ERROR · 12ms" in result.output
    assert "tool refused" in result.output and "never seen by a scheduler yet" in result.output


@pytest.mark.parametrize("command", ["pause", "resume"])
def test_unknown_pause_resume_target_is_named(hub, monkeypatch, command):
    monkeypatch.setattr(control, "set_paused", Mock(side_effect=control.ControlError("unknown", "unknown")))
    result = _invoke(command, hub, "md:missing")
    assert result.exit_code == 2 and "no task or workflow 'missing'" in result.output


@pytest.mark.parametrize(("error", "exit_code", "message"), [
    (None, 0, "cancel requested"),
    (control.ControlError("done", "finished", status="SUCCESS"), 0, "already SUCCESS; nothing to cancel"),
    (control.ControlError("unknown", "unknown_run"), 1, "no run 'run-1'"),
])
def test_cancel_handles_active_finished_and_unknown_runs(hub, monkeypatch, error, exit_code, message):
    monkeypatch.setattr(control, "cancel", Mock(side_effect=error))
    result = _invoke("cancel", hub, "run-1")
    assert result.exit_code == exit_code and message in result.output


@pytest.mark.parametrize(("definitions", "args", "message"), [
    ([{"name": "nightly", "error": "SyntaxError in workflow"}], [], "SyntaxError in workflow"),
    ([{"name": "nightly", "error": None}], ["--timeout", "10"], "Workflows do not accept"),
    ([], ["--input", "x=1"], "--input is only for code workflows"),
    ([], ["--dry-run"], "No workflow 'nightly' found"),
])
def test_workflow_run_early_refusals_do_not_start_engine(hub, monkeypatch, definitions, args, message):
    monkeypatch.setattr(observe, "definitions", lambda path: definitions)
    start = Mock(side_effect=AssertionError("should refuse before launch"))
    monkeypatch.setattr(runtime, "init", start)
    result = _invoke("run", hub, "nightly", *args)
    assert result.exit_code == 2 and message in result.output
    start.assert_not_called()


def test_workflow_dry_run_names_code_and_executes_nothing(hub, monkeypatch):
    monkeypatch.setattr(observe, "definitions", lambda path: [
        {"name": "nightly", "error": None, "source": "workflows/nightly/main.py"}])
    result = _invoke("run", hub, "nightly", "--dry-run")
    assert result.exit_code == 0 and "Would run workflow nightly from workflows/nightly/main.py; no code executed." in result.output


def test_workflow_run_uses_live_handle_and_reports_result(hub, monkeypatch):
    monkeypatch.setattr(observe, "definitions", lambda path: [{"name": "nightly", "error": None}])
    monkeypatch.setattr(cli, "_load_settings", lambda path: None)
    monkeypatch.setattr(cli, "_cli_envelope", lambda *a: {"trigger": "cli"})
    monkeypatch.setattr(context, "configure", Mock())
    handle = SimpleNamespace(get_workflow_id=lambda: "run-1", get_result=lambda: {"done": 3})
    enqueue = Mock(return_value=handle)
    monkeypatch.setattr(client, "enqueue_live", enqueue)
    monkeypatch.setattr(cli, "_audit_cli_start", Mock())
    monkeypatch.setattr(cli, "_print_report_links", Mock())
    result = _invoke("run", hub, "nightly")
    assert result.exit_code == 0 and "Workflow nightly: {'done': 3}" in result.output
    assert enqueue.call_args.args == (hub.resolve(), "nightly")
    assert enqueue.call_args.kwargs == {"envelope": {"trigger": "cli"}}


def test_workflow_run_failure_shuts_down_engine_and_explains_history(hub, monkeypatch):
    monkeypatch.setattr(observe, "definitions", lambda path: [{"name": "nightly", "error": None}])
    monkeypatch.setattr(cli, "_load_settings", lambda path: None)
    monkeypatch.setattr(cli, "_cli_envelope", lambda *a: {})
    monkeypatch.setattr(context, "configure", Mock())
    monkeypatch.setattr(client, "enqueue_live", lambda *a, **kw: None)
    for name in ("init", "load_workflows", "launch"):
        monkeypatch.setattr(runtime, name, Mock())
    monkeypatch.setattr(runtime, "registry", lambda: [SimpleNamespace(name="nightly")])
    monkeypatch.setattr(runtime, "start", Mock(side_effect=ValueError("bad input")))
    shutdown = Mock()
    monkeypatch.setattr(runtime, "shutdown", shutdown)
    result = _invoke("run", hub, "nightly")
    assert result.exit_code == 1 and "workflow nightly failed: ValueError: bad input" in result.output
    assert "The failed run is in" in result.output
    shutdown.assert_called_once()


def test_workflow_input_failure_is_reported_before_dispatch(hub, monkeypatch):
    monkeypatch.setattr(observe, "definitions", lambda path: [{"name": "nightly", "error": None}])
    monkeypatch.setattr(cli, "_cli_envelope", Mock(side_effect=ValueError("Input count must be an integer")))
    result = _invoke("run", hub, "nightly", "--input", "count=bad")
    assert result.exit_code == 2 and "Input count must be an integer" in result.output
