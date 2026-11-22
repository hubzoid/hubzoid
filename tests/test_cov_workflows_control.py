"""Starting a run now (hubzoid.workflows.control.start_now) in process, with
the engine's moving parts replaced: every refusal (no engine here, on a
runner, backup hold, switched-off task, broken or unloaded code workflow,
inputs a task doesn't take or a model rejects), an already running run, and
the envelope a team run carries. The engine-backed journeys are in
tests/test_workflow_control.py, whose engine processes are killed, so their
lines never reach the coverage report."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hubzoid.workflows import control, markdown, observe, runtime


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ("HUBZOID_DEPLOYMENT", "HUBZOID_SCHEDULES", "HUBZOID_GATEWAY", "WEBUI_AUTH",
                "HUBZOID_WORKFLOW_USER", "HUBZOID_DISABLE_SCHEDULE", "DATABASE_URL", "HUBZOID_ROLE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    from hubzoid import access, migrations

    access._stores.clear()
    migrations._done.clear()
    hub = tmp_path / "alpha"
    (hub / "schedule").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: alpha\ndescription: d\n---\nbody")
    (hub / "schedule" / "daily.md").write_text('---\nschedule: "0 3 * * *"\nrun: "true"\n---\n\nx\n')
    (hub / "schedule" / "paused.md").write_text('---\nschedule: "0 3 * * *"\nrun: "true"\nenabled: false\n---\n\nx\n')
    return hub


@pytest.fixture
def engine_here(hub, monkeypatch):
    """This process 'runs' the engine for the hub; runs are recorded, not run."""
    monkeypatch.setattr(runtime, "_LAUNCHED", True)
    monkeypatch.setattr(runtime, "_HUB_DIR", hub)
    started = []
    monkeypatch.setattr(markdown, "active_runs", lambda task: [])
    monkeypatch.setattr(markdown, "enqueue_task", lambda task, slot: (
        started.append(("md", task, slot)) or SimpleNamespace(get_workflow_id=lambda: f"md:{task}:{slot}")))
    return started


def test_no_engine_here_or_on_a_runner(hub, monkeypatch):
    monkeypatch.setattr(runtime, "_LAUNCHED", False)
    with pytest.raises(control.ControlError) as e:
        control.start_now(hub, "daily", actor="a@x", surface="cli")
    assert e.value.code == "not_running" and "isn't running in this agent" in e.value.message
    monkeypatch.setenv("HUBZOID_ROLE", "runner")
    with pytest.raises(control.ControlError) as e:
        control.start_now(hub, "daily", actor="a@x", surface="cli")
    assert e.value.code == "not_running" and e.value.message == control.RUNNER_NOT_HERE


def test_a_markdown_task_starts_once_and_is_audited(hub, engine_here, monkeypatch):
    audited = []
    monkeypatch.setattr(control, "_audit", lambda hub_dir, action, target, **f: audited.append((action, target, f)))
    out = control.start_now(hub, "daily", actor="ana@x", surface="console", request_id="req-1")
    assert out["kind"] == "markdown" and out["workflow"] == "md:daily" and out["already_running"] is False
    assert engine_here[0][0:2] == ("md", "daily") and engine_here[0][2].startswith("manual-")
    assert out["run_id"] == f"md:daily:{engine_here[0][2]}"
    assert audited == [("run_start", "md:daily", {"actor": "ana@x", "surface": "console",
                                                  "request_id": "req-1", "subject": out["run_id"]})]

    monkeypatch.setattr(markdown, "active_runs", lambda task: ["md:daily:running-1"])
    again = control.start_now(hub, "daily", actor="ana@x", surface="console")
    assert again["already_running"] is True and again["run_id"] == "md:daily:running-1"
    assert len(engine_here) == 1 and len(audited) == 1           # nothing new queued or recorded


def test_markdown_refusals(hub, engine_here, monkeypatch):
    with pytest.raises(control.ControlError) as e:
        control.start_now(hub, "paused", actor="a@x", surface="cli")
    assert e.value.code == "broken" and "enabled: false" in e.value.message
    with pytest.raises(control.ControlError) as e:
        control.start_now(hub, "daily", actor="a@x", surface="cli", inputs={"x": 1})
    assert e.value.code == "broken" and e.value.message == "Scheduled tasks don't take inputs."
    from hubzoid.access import store_for

    monkeypatch.setattr(type(store_for(hub)), "schedule_hold", lambda self: True)
    with pytest.raises(control.ControlError) as e:
        control.start_now(hub, "daily", actor="a@x", surface="cli")
    assert e.value.code == "held" and "backup" in e.value.message
    assert engine_here == []


def test_code_workflow_refusals_and_the_team_envelope(hub, engine_here, monkeypatch):
    defs = [{"name": "nightly", "error": None}, {"name": "broken", "error": "SyntaxError"}]
    monkeypatch.setattr(observe, "definitions", lambda hub_dir: defs)
    monkeypatch.setattr(observe, "catalog", lambda hub_dir: [{"name": "nightly", "runs_as": {"account": "svc@x"}}])

    with pytest.raises(control.ControlError) as e:
        control.start_now(hub, "broken", actor="a@x", surface="cli")
    assert e.value.code == "broken" and "problem in its code" in e.value.message

    monkeypatch.setattr(runtime, "_REGISTRY", {})
    with pytest.raises(control.ControlError) as e:
        control.start_now(hub, "nightly", actor="a@x", surface="cli")
    assert e.value.code == "code_off" and "HUBZOID_SCHEDULES" in e.value.message
    monkeypatch.setenv("HUBZOID_SCHEDULES", "1")
    with pytest.raises(control.ControlError) as e:
        control.start_now(hub, "nightly", actor="a@x", surface="cli")
    assert e.value.code == "broken" and "was not loaded" in e.value.message

    from pydantic import BaseModel

    class Inputs(BaseModel):
        region: str

    monkeypatch.setattr(runtime, "_REGISTRY", {"nightly": SimpleNamespace(inputs=Inputs, max_input_bytes=10_000)})
    monkeypatch.setattr(runtime, "_DBOS", SimpleNamespace(list_workflows=lambda **kw: []))
    sent = []
    monkeypatch.setattr(runtime, "start", lambda name, envelope: (
        sent.append((name, envelope)) or SimpleNamespace(get_workflow_id=lambda: "run-42")))
    monkeypatch.setattr(control, "_audit", lambda *a, **k: None)
    with pytest.raises(control.ControlError) as e:
        control.start_now(hub, "nightly", actor="a@x", surface="cli", inputs={"region": 5, "extra": 1})
    assert e.value.code == "bad_input"
    out = control.start_now(hub, "nightly", actor="ana@x", surface="api", inputs={"region": "eu"})
    assert out == {"workflow": "nightly", "kind": "code", "runs_as": "svc@x", "run_id": "run-42",
                   "already_running": False}
    name, envelope = sent[0]
    assert name == "nightly" and envelope["scope"] == "team" and envelope["started_by"] == "ana@x"
    assert envelope["via"] == "api" and envelope["trigger"] == "operator"
    assert envelope["inputs"] == {"region": "eu"}
