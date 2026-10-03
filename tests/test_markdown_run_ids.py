"""Markdown task and scheduled eval run ids are namespaced by hub.

A DBOS workflow id is global in its system database. Before the namespace, two
hubs sharing one PostgreSQL DBOS database both queued `md:<task>:<slot>`; the
second hub got the first hub's run back, so its task never ran and it reported
the other hub's result. Ids written before the namespace are still read.

DBOS is a process-global singleton, so each hub runs in its own subprocess.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid

import pytest

from hubzoid.workflows import markdown, runtime


def test_run_ids_carry_the_hub():
    alpha = markdown.run_id("sync", "20261001T0300", "alpha")
    beta = markdown.run_id("sync", "20261001T0300", "beta")
    assert alpha != beta
    assert alpha == f"md:sync:20261001T0300@{runtime._app_name('alpha')}"
    for run in (alpha, alpha + ":requeued", alpha + ":requeued:requeued"):
        assert markdown.task_name_from_id(run) == "sync"
    assert markdown.task_name_from_id(markdown.run_id("a:b", "events-20261001T0300-ab12", "hub")) == "a:b"


def test_ids_from_before_the_namespace_are_still_read():
    legacy = "md:sync:20260925T0300"
    assert markdown.task_name_from_id(legacy) == "sync"
    assert markdown.task_name_from_id(legacy + ":requeued") == "sync"
    # One prefix lists a task's runs in both forms; DBOS scopes it to the hub.
    prefix = markdown.run_prefix("sync")
    assert legacy.startswith(prefix)
    assert markdown.run_id("sync", "s1", "hub").startswith(prefix)
    assert not markdown.run_id("sync-2", "s1", "hub").startswith(prefix)


def test_eval_suite_ids_carry_the_hub():
    alpha = markdown.eval_run_id(["weekly", "canary"], "20261001T0300", "alpha")
    assert alpha == f"eval:canary,weekly:20261001T0300@{runtime._app_name('alpha')}"
    assert alpha != markdown.eval_run_id(["canary", "weekly"], "20261001T0300", "beta")


def test_hubs_whose_names_differ_only_in_punctuation_do_not_collide():
    assert markdown.run_id("t", "s", "team.alpha") != markdown.run_id("t", "s", "team-alpha")


def test_the_namespace_defaults_to_the_initialised_hub(monkeypatch):
    monkeypatch.setattr(runtime, "_HUB_NAME", "")
    with pytest.raises(RuntimeError, match="init"):
        markdown.run_id("sync", "s1")
    monkeypatch.setattr(runtime, "_HUB_NAME", "finance")
    assert markdown.run_id("sync", "s1") == markdown.run_id("sync", "s1", "finance")


_ENQUEUE = textwrap.dedent('''
    import json, sys
    from datetime import datetime
    from pathlib import Path
    from hubzoid.workflows import markdown, observe, runtime
    hub = Path(sys.argv[1])
    runtime.init(hub)
    runtime.launch()
    task = markdown.enqueue_task("sync", "20261001T0300")
    out = task.get_result()
    suite = markdown.enqueue_evals(["nightly"], datetime(2026, 10, 1, 3, 0))
    suite_out = suite.get_result()
    rows = observe.runs(hub, name="md:sync", trusted=True)
    print("OUT " + json.dumps({"result": out["result"], "task_id": task.get_workflow_id(),
                               "eval_id": suite.get_workflow_id(), "eval": suite_out,
                               "runs": [[r["id"], r["name"]] for r in rows]}), flush=True)
    runtime.shutdown()
''')


@pytest.fixture
def own_postgres(postgres_url):
    """A database of its own on the session's PostgreSQL server: the shared one
    keeps the state the migration tests expect."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = f"hz_run_ids_{uuid.uuid4().hex[:8]}"
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        yield make_url(postgres_url).set(database=name).render_as_string(hide_password=False)
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@pytest.mark.slow
def test_two_hubs_sharing_postgres_both_run_the_same_task_and_slot(own_postgres, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "HUBZOID_DEPLOYMENT")}
    env.update(HUBZOID_DBOS_DB=own_postgres, HUBZOID_OPERATIONAL_DB=own_postgres)
    results = {}
    for name in ("alpha", "beta"):
        hub = tmp_path / name
        (hub / "schedule").mkdir(parents=True)
        (hub / "AGENTS.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody")
        (hub / "schedule" / "sync.md").write_text(
            f'---\nrun: "echo {name} > ran.txt"\nschedule: "0 3 * * *"\n---\n\nx\n')
        proc = subprocess.run([sys.executable, "-c", _ENQUEUE, str(hub)], env=env,
                              capture_output=True, text=True, timeout=180)
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("OUT ")), None)
        assert line, proc.stderr[-3000:]
        results[name] = json.loads(line[4:])
        # The task ran in this hub, not only in the first one.
        assert (hub / "ran.txt").read_text().strip() == name

    for name, res in results.items():
        expected = markdown.run_id("sync", "20261001T0300", name)
        assert res["result"] == "done"
        assert res["task_id"] == expected
        assert res["runs"] == [[expected, "md:sync"]]  # each hub lists only its own run
        assert res["eval"] == {"cases": 0}
    assert results["alpha"]["eval_id"] != results["beta"]["eval_id"]


_LEGACY = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    from dbos import SetWorkflowID
    from hubzoid.workflows import markdown, observe, runtime
    hub = Path(sys.argv[1])
    runtime.init(hub)
    runtime.launch()
    # A run queued by an earlier release, under the id form without a hub.
    with SetWorkflowID("md:slow:20260925T0300"):
        legacy = runtime._MD_QUEUE.enqueue(markdown._FNS["md_task"], "slow", [], {})
    deadline = time.time() + 60
    active = []
    while time.time() < deadline and not active:
        active = markdown.active_runs("slow")
        time.sleep(0.2)
    legacy.get_result()
    new = markdown.enqueue_task("slow", "20260926T0300")
    new.get_result()
    rows = observe.runs(hub, name="md:slow", trusted=True)
    print("OUT " + json.dumps({"active": active, "new": new.get_workflow_id(),
                               "rows": sorted([r["id"], r["name"], r["status"]] for r in rows)}),
          flush=True)
    runtime.shutdown()
''')


@pytest.mark.slow
def test_runs_queued_before_the_namespace_are_still_seen(tmp_path):
    hub = tmp_path / "hub"
    (hub / "schedule").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: hub\ndescription: d\n---\nbody")
    (hub / "schedule" / "slow.md").write_text(
        '---\nrun: "sleep 2; echo x >> ran.txt"\nschedule: "0 3 * * *"\n---\n\nx\n')
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "HUBZOID_DEPLOYMENT", "HUBZOID_DBOS_DB")}
    env["HUBZOID_OPERATIONAL_DB"] = f"sqlite:///{tmp_path / 'ops.db'}"
    proc = subprocess.run([sys.executable, "-c", _LEGACY, str(hub)], env=env,
                          capture_output=True, text=True, timeout=180)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("OUT ")), None)
    assert line, proc.stderr[-3000:]
    out = json.loads(line[4:])
    assert out["active"] == ["md:slow:20260925T0300"]  # the scheduler still sees it running
    assert out["new"] == markdown.run_id("slow", "20260926T0300", "hub")
    assert out["rows"] == sorted([["md:slow:20260925T0300", "md:slow", "SUCCESS"],
                                  [out["new"], "md:slow", "SUCCESS"]])
    assert (hub / "ran.txt").read_text().count("x") == 2
