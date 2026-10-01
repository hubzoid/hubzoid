"""Run controls shared by the CLI and the agent tools (`workflows/control.py`).

Model-free, on SQLite. Starting runs needs a launched DBOS engine, which is a
process-global singleton, so those cases run the engine in a subprocess (as
test_markdown_on_dbos does); reads, pauses and cancels from outside use the
database like the CLI.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from hubzoid.workflows import control

pytestmark = pytest.mark.skipif(os.name == "nt", reason="uses process groups")

_CODE = textwrap.dedent('''
    import os, time
    from hubzoid import workflow

    @workflow()
    def nightly_sync():
        time.sleep(float(os.environ.get("NIGHTLY_SLEEP", "0")))
        return "ok"
''')

_BROKEN = textwrap.dedent('''
    from hubzoid import workflow

    @workflow(schedule="whenever it likes")
    def broken():
        return 1
''')

# argv: hub, "code" to load code workflows, then a JSON plan of operations.
_ENGINE = textwrap.dedent('''
    import json, os, sys
    from hubzoid.access import store_for
    from hubzoid.workflows import control, runtime
    hub, code, plan = sys.argv[1], sys.argv[2] == "code", json.loads(sys.argv[3])
    runtime.init(hub)
    if code:
        runtime.load_workflows(hub)
    runtime.launch()
    out = []
    for op, *args in plan:
        try:
            if op == "start":
                out.append(control.start_now(hub, args[0], actor="ann@x.org", surface="owui",
                                             request_id="chat-1"))
            elif op == "cancel":
                run = out[args[0]]["run_id"]
                out.append(control.cancel(hub, run, actor="ann@x.org", surface="mcp"))
            elif op == "wait":
                try:
                    runtime._DBOS.retrieve_workflow(out[args[0]]["run_id"]).get_result()
                except Exception:
                    pass
                out.append(None)
            elif op == "hold":
                store_for(hub).set_schedule_hold("backup", 60, actor="test")
                out.append(None)
        except control.ControlError as exc:
            out.append({"error": exc.code, "message": exc.message})
    print("OUT " + json.dumps(out), flush=True)
    os._exit(0)
''')

_QUEUE = textwrap.dedent('''
    import sys, time
    from hubzoid.workflows import markdown, runtime
    runtime.init(sys.argv[1])
    runtime.launch()
    markdown.enqueue_task("quick", "q1").get_result()
    markdown.enqueue_task("slow", "s1")
    markdown.enqueue_task("daily", "s2")      # queued behind the slow one
    print("QUEUED", flush=True)
    time.sleep(90)
''')


def _task(hub, name, body):
    (hub / "schedule" / f"{name}.md").write_text(f"---\n{body}\n---\n\nx\n")


def _hub(root, name):
    hub = root / name
    (hub / "schedule").mkdir(parents=True)
    (hub / "AGENTS.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody")
    return hub


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("HUBZOID_DEPLOYMENT", "HUBZOID_SCHEDULES", "HUBZOID_GATEWAY", "WEBUI_AUTH",
                "HUBZOID_WORKFLOW_USER", "HUBZOID_DISABLE_SCHEDULE", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_DBOS_DB", f"sqlite:///{tmp_path / 'dbos.db'}")
    from hubzoid import access, migrations

    access._stores.clear()
    migrations._done.clear()
    return dict(os.environ)


@pytest.fixture
def hub(tmp_path, env):
    hub = _hub(tmp_path, "alpha")
    _task(hub, "daily", 'schedule: "0 3 * * *"\nrun: "sleep 30"\nrun_as: carol@x.org')
    (hub / "workflows" / "nightly").mkdir(parents=True)
    (hub / "workflows" / "nightly" / "main.py").write_text(_CODE)
    from hubzoid.access import store_for

    store_for(hub).upsert_identity(email="carol@x.org", owui_id="id-carol")
    return hub


def _engine(hub, env, plan, *, code=True):
    proc = subprocess.Popen([sys.executable, "-c", _ENGINE, str(hub), "code" if code else "-",
                             json.dumps(plan)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=180)
    finally:
        _kill_group(proc)
    line = next((ln for ln in stdout.splitlines() if ln.startswith("OUT ")), None)
    assert line, stderr[-3000:]
    return json.loads(line[4:])


def _kill_group(proc):
    # Also stops a task's `sleep` left running when the engine process exits.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait(timeout=30)


def _audit(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    with eng.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT actor, action, subject, hub, permission, surface, request_id "
            "FROM hz_access_audit")).mappings()]


# ---- resolve ------------------------------------------------------------------------

def test_resolve_both_kinds_and_unknown(hub):
    assert control.resolve(hub, "daily") == control.Target("md:daily", "markdown", "daily")
    assert control.resolve(hub, "md:daily") == control.Target("md:daily", "markdown", "daily")
    assert control.resolve(hub, "nightly_sync") == control.Target("nightly_sync", "code")
    assert control.resolve(hub, "nightly-sync") == control.Target("nightly_sync", "code")
    for name in ("nope", "", "md:"):
        with pytest.raises(control.ControlError) as e:
            control.resolve(hub, name)
        assert e.value.code == "unknown"


# ---- start now (engine in a subprocess) ----------------------------------------------

def test_start_queues_one_run_and_returns_the_active_one(hub, env, tmp_path):
    out = _engine(hub, dict(env, NIGHTLY_SLEEP="30"), [
        ["start", "daily"], ["start", "md:daily"],
        ["start", "nightly-sync"], ["start", "nightly_sync"],
        ["cancel", 0], ["cancel", 2],
    ])
    md, md_again, code, code_again, md_cancel, code_cancel = out
    assert md["kind"] == "markdown" and md["workflow"] == "md:daily"
    assert md["run_id"].startswith("md:daily:manual-") and md["run_id"].endswith("@alpha")
    assert md["already_running"] is False and md["runs_as"] == "carol@x.org"
    assert md_again == dict(md, already_running=True)
    assert code["kind"] == "code" and code["workflow"] == "nightly_sync"
    assert code["already_running"] is False
    # The run acts as the workflow's own account (here the local owner), never the caller.
    assert code["runs_as"] and code["runs_as"] != "ann@x.org"
    assert code_again == dict(code, already_running=True)
    assert md_cancel["run_id"] == md["run_id"] and md_cancel["previous"] in control.ACTIVE
    assert code_cancel["workflow"] == "nightly_sync"

    rows = _audit(tmp_path)
    starts = [r for r in rows if r["action"] == "run_start"]
    # One audit row per run actually started; the "already running" replies write none.
    assert starts == [
        dict(actor="ann@x.org", action="run_start", subject=md["run_id"], hub="alpha",
             permission="md:daily", surface="owui", request_id="chat-1"),
        dict(actor="ann@x.org", action="run_start", subject=code["run_id"], hub="alpha",
             permission="nightly_sync", surface="owui", request_id="chat-1"),
    ]
    cancels = [(r["permission"], r["actor"], r["surface"]) for r in rows if r["action"] == "run_cancel"]
    assert cancels == [(md["run_id"], "ann@x.org", "mcp"), (code["run_id"], "ann@x.org", "mcp")]

    from dbos import DBOSClient

    from hubzoid import db
    from hubzoid.workflows.runtime import _app_name

    client = DBOSClient(system_database_url=db.dbos_url(hub), application_name=_app_name(hub.name))
    try:
        assert client.retrieve_workflow(md["run_id"]).get_status().status == "CANCELLED"
    finally:
        client.destroy()


def test_start_refusals(hub, env, tmp_path):
    (hub / "workflows" / "broken").mkdir()
    (hub / "workflows" / "broken" / "main.py").write_text(_BROKEN)
    _task(hub, "off", 'schedule: "0 3 * * *"\nrun: "true"\nenabled: false')
    out = _engine(hub, env, [
        ["start", "nightly_sync"],   # the engine runs, but without code workflows
        ["start", "broken"],
        ["start", "off"],
        ["hold"],
        ["start", "daily"],
    ], code=False)
    code_off, broken, off, _, held = out
    assert code_off == {"error": "code_off", "message":
                        "Code workflows are off on this agent (HUBZOID_SCHEDULES is not set)."}
    assert broken["error"] == "broken" and "problem in its code" in broken["message"]
    assert off["error"] == "broken" and "enabled: false" in off["message"]
    assert held["error"] == "held" and "backup" in held["message"]
    assert not [r for r in _audit(tmp_path) if r["action"] == "run_start"]


def test_start_needs_this_hubs_engine(hub, tmp_path, monkeypatch):
    from hubzoid.workflows import runtime

    with pytest.raises(control.ControlError) as e:
        control.start_now(hub, "daily", actor="ann@x.org", surface="owui")
    assert e.value.code == "not_running" and "isn't running in this agent" in e.value.message
    # An engine launched in this process for a different hub doesn't count.
    monkeypatch.setattr(runtime, "_LAUNCHED", True)
    monkeypatch.setattr(runtime, "_HUB_DIR", _hub(tmp_path, "other"))
    with pytest.raises(control.ControlError) as e:
        control.start_now(hub, "daily", actor="ann@x.org", surface="owui")
    assert e.value.code == "not_running"
    with pytest.raises(control.ControlError) as e:
        control.start_now(hub, "nope", actor="ann@x.org", surface="owui")
    assert e.value.code == "unknown"


# ---- pause and resume ------------------------------------------------------------------

def test_pause_and_resume_are_audited_with_actor_and_surface(hub, tmp_path):
    from hubzoid.access import store_for

    assert control.set_paused(hub, "nightly-sync", True, actor="ann@x.org", surface="whatsapp",
                              request_id="chat-9") == dict(workflow="nightly_sync", paused=True,
                                                           changed=True)
    assert control.set_paused(hub, "nightly_sync", True, actor="ann@x.org",
                              surface="owui")["changed"] is False
    assert store_for(hub).paused_workflows("alpha") == {"nightly_sync"}
    assert control.set_paused(hub, "md:daily", False, actor="bob@x.org", surface="cli") == dict(
        workflow="md:daily", paused=False, changed=False)
    assert control.set_paused(hub, "nightly_sync", False, actor="bob@x.org",
                              surface="mcp")["changed"] is True
    assert store_for(hub).paused_workflows("alpha") == set()
    rows = [(r["action"], r["permission"], r["actor"], r["surface"], r["request_id"])
            for r in _audit(tmp_path)]
    assert rows == [
        ("workflow_pause", "nightly_sync", "ann@x.org", "whatsapp", "chat-9"),
        ("workflow_pause", "nightly_sync", "ann@x.org", "owui", None),
        ("workflow_resume", "md:daily", "bob@x.org", "cli", None),
        ("workflow_resume", "nightly_sync", "bob@x.org", "mcp", None),
    ]
    with pytest.raises(control.ControlError) as e:
        control.set_paused(hub, "nope", True, actor="ann@x.org", surface="owui")
    assert e.value.code == "unknown"


# ---- listing and history ---------------------------------------------------------------

def test_overview_and_history_without_runs(hub, monkeypatch):
    rows = {r["name"]: r for r in control.overview(hub, viewer="ann@x.org")}
    assert rows["md:daily"]["kind"] == "markdown" and rows["nightly_sync"]["kind"] == "code"
    assert rows["md:daily"]["last_run"] is None
    assert rows["md:daily"]["runs_as"]["account"] == "carol@x.org"

    from hubzoid.workflows import observe

    seen, down = [], [True]

    def runs(hub_dir, **kw):
        seen.append(kw)
        if down[0]:
            raise RuntimeError("database is locked")
        return []

    monkeypatch.setattr(observe, "runs", runs)
    listed = control.overview(hub, viewer="ann@x.org")   # history down: listing still works
    assert {r["name"] for r in listed} == {"md:daily", "nightly_sync"}
    assert all(r["last_run"] is None for r in listed)

    seen.clear()
    down[0] = False
    control.history(hub, viewer="ann@x.org", workflow="daily", status="failed", limit=500)
    control.history(hub, viewer="ann@x.org", limit=0)
    assert seen[0]["name"] == "md:daily" and seen[0]["limit"] == 25
    assert seen[0]["statuses"] == ["ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"]
    assert seen[0]["viewer"] == "ann@x.org"
    assert seen[1]["limit"] == 1 and seen[1]["name"] is None
    with pytest.raises(control.ControlError) as e:
        control.history(hub, viewer="ann@x.org", status="exploded")
    assert e.value.code == "bad_filter"


# ---- cancel from outside the engine (two hubs, one DBOS database) ----------------------

def test_cancel_is_scoped_to_this_hub_and_refuses_finished_runs(tmp_path, env):
    from hubzoid import cli

    alpha, beta = _hub(tmp_path, "alpha"), _hub(tmp_path, "beta")
    _task(alpha, "quick", 'schedule: "0 3 * * *"\nrun: "true"')
    _task(alpha, "slow", 'schedule: "0 4 * * *"\nrun: "sleep 60"')
    _task(alpha, "daily", 'schedule: "0 5 * * *"\nrun: "true"')
    _task(beta, "daily", 'schedule: "0 5 * * *"\nrun: "true"')
    proc = subprocess.Popen([sys.executable, "-c", _QUEUE, str(alpha)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    try:
        assert proc.stdout.readline().startswith("QUEUED"), proc.stderr.read()[-3000:] \
            if proc.poll() is not None else "not queued"
        queued, done = "md:daily:s2@alpha", "md:quick:q1@alpha"

        # beta shares the database but can't see, let alone cancel, alpha's run.
        with pytest.raises(control.ControlError) as e:
            control.cancel(beta, queued, actor="bob@x.org", surface="owui")
        assert e.value.code == "unknown_run"
        with pytest.raises(control.ControlError) as e:
            control.cancel(alpha, done, actor="bob@x.org", surface="owui")
        assert e.value.code == "finished" and e.value.status == "SUCCESS"
        assert "already finished (SUCCESS)" in e.value.message

        r = CliRunner().invoke(cli.app, ["schedule", "cancel", str(alpha), done])
        assert r.exit_code == 0 and "already SUCCESS; nothing to cancel" in r.output
        r = CliRunner().invoke(cli.app, ["schedule", "cancel", str(beta), queued])
        assert r.exit_code == 1 and "no run" in r.output

        out = control.cancel(alpha, queued, actor="bob@x.org", surface="whatsapp")
        assert out == dict(run_id=queued, workflow="md:daily", previous="ENQUEUED")
        from dbos import DBOSClient

        from hubzoid import db
        from hubzoid.workflows.runtime import _app_name

        client = DBOSClient(system_database_url=db.dbos_url(alpha),
                            application_name=_app_name(alpha.name))
        try:
            assert client.retrieve_workflow(queued).get_status().status == "CANCELLED"
        finally:
            client.destroy()

        rows = {r["name"]: r for r in control.overview(alpha, viewer="bob@x.org")}
        assert rows["md:quick"]["last_run"]["status"] == "SUCCESS"
        assert rows["md:daily"]["last_run"]["status"] == "CANCELLED"
        assert [r["id"] for r in control.history(alpha, viewer=None, workflow="quick")] == [done]
        assert control.history(beta, viewer=None) == []
    finally:
        _kill_group(proc)
    cancels = [(r["permission"], r["actor"], r["surface"])
               for r in _audit(tmp_path) if r["action"] == "run_cancel"]
    assert cancels == [(queued, "bob@x.org", "whatsapp")]


# ---- review fixes -------------------------------------------------------------------

def test_markdown_starts_refused_for_webhook_tasks_and_the_kill_switch(hub, monkeypatch):
    _task(hub, "inbox", 'on_webhook: squadcast\nrun: "true"')
    with pytest.raises(control.ControlError) as e:
        control._check_startable(hub, control.resolve(hub, "inbox"))
    assert e.value.code == "event" and "squadcast" in e.value.message
    control._check_startable(hub, control.resolve(hub, "daily"))  # a scheduled task is fine
    monkeypatch.setenv("HUBZOID_DISABLE_SCHEDULE", "1")
    with pytest.raises(control.ControlError) as e:
        control._check_startable(hub, control.resolve(hub, "daily"))
    assert e.value.code == "broken" and "HUBZOID_DISABLE_SCHEDULE" in e.value.message


def test_a_bare_name_shared_by_both_kinds(hub):
    """The CLI keeps its markdown-first rule (as `schedule run` resolves); the
    tools prefer the code workflow, since their listing shows `md:<task>`."""
    _task(hub, "nightly_sync", 'schedule: "0 3 * * *"\nrun: "true"')
    md = control.Target("md:nightly_sync", "markdown", "nightly_sync")
    assert control.resolve(hub, "nightly_sync") == md
    assert control.resolve(hub, "nightly_sync", prefer="code") == control.Target("nightly_sync", "code")
    assert control.resolve(hub, "md:nightly_sync", prefer="code") == md
    assert control.resolve(hub, "nightly-sync", prefer="markdown") == control.Target("nightly_sync", "code")


def test_history_keeps_legacy_service_runs_private_and_reads_queued(hub, monkeypatch):
    from hubzoid.workflows import observe

    seen = {}
    monkeypatch.setattr(observe, "runs", lambda *a, **k: seen.update(k) or [])
    control.history(hub, viewer="ann@x.org", status="queued")
    assert seen["legacy_visible"] is False and seen["statuses"] == ["ENQUEUED"]
    legacy = {"source": "legacy-service", "subject": "workflow:x"}
    assert observe.may_see_results(legacy, "ann@x.org") is True  # the Console's managers
    assert observe.may_see_results(legacy, "ann@x.org", legacy_visible=False) is False


def test_each_manual_start_is_a_new_run_even_within_a_second(hub, env, tmp_path):
    """A start right after a short run finished must run again, not reuse the
    finished run's id (DBOS would dedupe it and nothing would run)."""
    import re

    _task(hub, "quick", 'schedule: "0 3 * * *"\nrun: "true"\nrun_as: carol@x.org')
    first, _, second = _engine(hub, env, [["start", "quick"], ["wait", 0], ["start", "quick"]],
                               code=False)
    assert first["already_running"] is False and second["already_running"] is False
    assert first["run_id"] != second["run_id"]
    assert re.fullmatch(r"md:quick:manual-\d{8}T\d{6}-[0-9a-f]{6}@alpha", second["run_id"])
    assert [r["subject"] for r in _audit(tmp_path) if r["action"] == "run_start"] == [
        first["run_id"], second["run_id"]]
