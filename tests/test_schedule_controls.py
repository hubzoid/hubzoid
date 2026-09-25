"""Run controls are CLI only (`hubzoid schedule pause | resume | cancel`), act
with the server operator's authority, persist in the shared store so every
bridge sees them, and are audited."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from hubzoid import cli
from hubzoid import scheduler as scheduler_lib
from hubzoid import scheduling as sch


@pytest.fixture
def hub(tmp_path, monkeypatch):
    h = tmp_path / "hub"
    (h / "schedule").mkdir(parents=True)
    (h / "schedule" / "daily.md").write_text('---\nschedule: "0 3 * * *"\nrun: "true"\n---\n\nx\n')
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_DBOS_DB", f"sqlite:///{tmp_path / 'dbos.db'}")
    monkeypatch.delenv("HUBZOID_DEPLOYMENT", raising=False)
    from hubzoid import access, migrations

    access._stores.clear()
    migrations._done.clear()
    return h


def _audit(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    with eng.connect() as c:
        return [tuple(r) for r in c.execute(text("SELECT actor, action, hub, permission FROM hz_access_audit"))]


def test_pause_stops_dispatch_and_resume_catches_up_once(hub, tmp_path):
    queued = []
    s = scheduler_lib.Scheduler(hub, dispatch_task=lambda t, slot, c: queued.append(t.name))
    sch.ScheduleState(hub).record_fired("daily", datetime.now() - timedelta(days=3), result="done")

    r = CliRunner().invoke(cli.app, ["schedule", "pause", str(hub), "daily"])
    assert r.exit_code == 0, r.output
    assert asyncio.run(s.check_once()) == [] and queued == []   # due, but paused

    from hubzoid.workflows import observe

    (row,) = [x for x in observe.markdown_catalog(hub) if x["name"] == "md:daily"]
    assert row["state"] == "paused"

    assert CliRunner().invoke(cli.app, ["schedule", "resume", str(hub), "daily"]).exit_code == 0
    assert asyncio.run(s.check_once()) == ["daily"]              # one catch-up run
    assert asyncio.run(s.check_once()) == []

    actions = [(a[1], a[3]) for a in _audit(tmp_path)]
    assert ("workflow_pause", "md:daily") in actions and ("workflow_resume", "md:daily") in actions
    assert all(a[0].startswith("cli:") for a in _audit(tmp_path))


def test_unknown_name_is_refused(hub):
    r = CliRunner().invoke(cli.app, ["schedule", "pause", str(hub), "nope"])
    assert r.exit_code == 2 and "no task or workflow" in r.output


_QUEUE_BLOCKED = textwrap.dedent('''
    import sys, time
    from hubzoid.workflows import markdown, runtime
    runtime.init(sys.argv[1])
    runtime.launch()
    markdown.enqueue_task("slow", "s1")
    markdown.enqueue_task("daily", "s2")      # queued behind the slow one
    print("QUEUED", flush=True)
    time.sleep(90)
''')


@pytest.mark.skipif(os.name == "nt", reason="uses a background process")
def test_cancel_a_queued_run(hub, tmp_path):
    (hub / "schedule" / "slow.md").write_text('---\nschedule: "0 4 * * *"\nrun: "sleep 60"\n---\n\nx\n')
    proc = subprocess.Popen([sys.executable, "-c", _QUEUE_BLOCKED, str(hub)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=dict(os.environ))
    try:
        assert proc.stdout.readline().startswith("QUEUED")
        r = CliRunner().invoke(cli.app, ["schedule", "cancel", str(hub), "md:daily:s2"])
        assert r.exit_code == 0, r.output
        from dbos import DBOSClient

        from hubzoid import db
        from hubzoid.workflows.runtime import _app_name

        client = DBOSClient(system_database_url=db.dbos_url(hub), application_name=_app_name(hub.name))
        try:
            assert client.retrieve_workflow("md:daily:s2").get_status().status == "CANCELLED"
        finally:
            client.destroy()
        assert ("run_cancel", "md:daily:s2") in [(a[1], a[3]) for a in _audit(tmp_path)]
    finally:
        proc.kill()
        proc.wait(timeout=30)
