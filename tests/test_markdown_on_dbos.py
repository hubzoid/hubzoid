"""Markdown schedule tasks run on the hub's DBOS engine (workflows/markdown.py).

Script (`run:`) tasks keep these tests model-free. Each case runs in a
subprocess because DBOS is a process-global singleton.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

_RUN = textwrap.dedent('''
    import json, os, sys
    from hubzoid.workflows import markdown, runtime
    hub, task, slot = sys.argv[1], sys.argv[2], sys.argv[3]
    claimed = json.loads(sys.argv[4]) if len(sys.argv) > 4 else []
    runtime.init(hub)
    runtime.launch()
    handle = markdown.enqueue_task(task, slot, claimed)
    try:
        out = handle.get_result()
        print("RESULT " + json.dumps(out))
    except Exception as exc:
        print("FAILED " + str(exc))
    from dbos import DBOS
    ids = [w.workflow_id for w in DBOS.list_workflows(workflow_id_prefix="md:")]
    print("IDS " + json.dumps(ids))
    runtime.shutdown()
''')


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A hub inside a git repo with a bare remote, like a deployed hub checkout."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(remote), str(work))
    _git(work, "config", "user.email", "t@example.org")
    _git(work, "config", "user.name", "t")
    hub = work / "hub"
    (hub / "schedule").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: hub\ndescription: d\n---\nbody")
    (hub / ".gitignore").write_text(".hubzoid/\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", "HEAD:main")
    _git(work, "branch", "--set-upstream-to=origin/main")
    env = dict(os.environ)
    env["HUBZOID_OPERATIONAL_DB"] = f"sqlite:///{tmp_path / 'ops.db'}"
    env["HUBZOID_DBOS_DB"] = f"sqlite:///{tmp_path / 'dbos.db'}"
    return hub, remote, env


def _task(hub, name, body):
    (hub / "schedule" / f"{name}.md").write_text(f"---\n{body}\n---\n\nx\n")


def _run(hub, env, task, slot, claimed=None):
    args = [sys.executable, "-c", _RUN, str(hub), task, slot]
    if claimed is not None:
        args.append(json.dumps(claimed))
    proc = subprocess.run(args, capture_output=True, text=True, timeout=180, env=env)
    lines = proc.stdout.splitlines()
    res = next((ln for ln in lines if ln.startswith(("RESULT ", "FAILED "))), None)
    ids = next((ln for ln in lines if ln.startswith("IDS ")), "IDS []")
    assert res, proc.stderr[-2000:]
    ok = res.startswith("RESULT ")
    return (json.loads(res[7:]) if ok else res[7:]), json.loads(ids[4:])


def test_script_task_commits_and_pushes(repo):
    hub, remote, env = repo
    _task(hub, "sync", 'run: "echo synced > data.txt"\ncommit: ["data.txt"]\npush: true\nschedule: "0 3 * * *"')
    out, ids = _run(hub, env, "sync", "20260925T0300")
    assert out["result"] == "done" and out["commit_sha"] and out["pushed"] is True
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline", "-1"],
                         capture_output=True, text=True).stdout
    assert "schedule(sync)" in log  # the commit reached the remote
    assert ids == ["md:sync:20260925T0300"]
    from hubzoid import scheduling as sch

    assert sch.ScheduleState(hub).get("sync")["last_result"] == "done"


def test_nothing_to_commit_is_not_an_error(repo):
    hub, _, env = repo
    _task(hub, "noop", 'run: "true"\ncommit: ["data.txt"]\nschedule: "0 3 * * *"')
    out, _ = _run(hub, env, "noop", "s1")
    assert out["result"] == "done" and out["commit_sha"] is None


def test_same_slot_runs_once(repo):
    hub, _, env = repo
    _task(hub, "count", 'run: "echo x >> count.txt"\nschedule: "0 3 * * *"')
    _run(hub, env, "count", "slot-a")
    _run(hub, env, "count", "slot-a")  # e.g. two processes saw the same slot due
    assert (hub / "count.txt").read_text().count("x") == 1


def test_failing_script_fails_the_run(repo):
    hub, _, env = repo
    _task(hub, "bad", 'run: "exit 3"\nschedule: "0 3 * * *"')
    out, _ = _run(hub, env, "bad", "s1")
    assert isinstance(out, str) and "exit 3" in out


def _events(hub, name, n):
    from hubzoid.inbound.webhook import make_file_sink, pending_events

    for i in range(n):
        make_file_sink(hub, name)({"surface": "webhook", "name": name, "body": {"n": i}})
    return [str(p) for p in pending_events(hub, name)]


def test_webhook_events_archived_only_when_done(repo):
    hub, _, env = repo
    from hubzoid.inbound.webhook import pending_events

    _task(hub, "alerts", 'on_webhook: squadcast\nrun: "exit 1"')
    claimed = _events(hub, "squadcast", 1)
    _run(hub, env, "alerts", "events-1", claimed)
    assert [str(p) for p in pending_events(hub, "squadcast")] == claimed  # kept for retry

    _task(hub, "alerts", 'on_webhook: squadcast\nrun: "true"')
    _run(hub, env, "alerts", "events-2", claimed)
    assert pending_events(hub, "squadcast") == []  # archived after DONE


def test_a_run_handles_only_the_events_it_claimed(repo):
    """An event that lands after the claim is not given to the run, and stays
    pending for the next one."""
    hub, _, env = repo
    from hubzoid.inbound.webhook import pending_events

    _task(hub, "alerts", 'on_webhook: squadcast\nrun: \'printf "%s" "$HUBZOID_WEBHOOK_EVENTS" > got.txt\'')
    claimed = _events(hub, "squadcast", 1)
    later = _events(hub, "squadcast", 1)[-1]  # lands after the scheduler claimed
    out, _ = _run(hub, env, "alerts", "events-1", claimed)
    assert out["result"] == "done"
    assert (hub / "got.txt").read_text() == claimed[0]
    assert [str(p) for p in pending_events(hub, "squadcast")] == [later]


def test_a_run_with_nothing_to_commit_pushes_nothing(repo):
    """`push: true` publishes the run's own commit only. With no change there is
    no commit, so an unrelated unpushed local commit stays local."""
    hub, remote, env = repo
    (hub / "notes.txt").write_text("operator draft")
    _git(hub.parent, "add", "hub/notes.txt")
    _git(hub.parent, "commit", "-m", "operator draft not for pushing")
    _task(hub, "noop", 'run: "true"\ncommit: ["data.txt"]\npush: true\nschedule: "0 3 * * *"')
    out, _ = _run(hub, env, "noop", "s1")
    assert out["result"] == "done" and out["commit_sha"] is None and not out.get("pushed")
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "operator draft" not in log


_SCHEDULER = textwrap.dedent('''
    import asyncio, json, sys
    from datetime import datetime, timedelta
    from dbos import DBOS
    from hubzoid import scheduler as scheduler_lib
    from hubzoid.workflows import runtime
    runtime.init(sys.argv[1])
    runtime.launch()

    def settle():
        for w in DBOS.list_workflows(workflow_id_prefix="md:alerts:"):
            try:
                DBOS.retrieve_workflow(w.workflow_id).get_result()
            except Exception:
                pass

    s = scheduler_lib.Scheduler(sys.argv[1])
    t0 = datetime(2026, 9, 25, 10, 0, 5)
    first = asyncio.run(s.check_once(t0))
    during = asyncio.run(s.check_once(t0 + timedelta(minutes=1)))
    settle()
    retried = asyncio.run(s.check_once(t0 + timedelta(minutes=2)))
    settle()
    ids = [w.workflow_id for w in DBOS.list_workflows(workflow_id_prefix="md:alerts:")]
    print("OUT " + json.dumps([first, during, retried, ids]))
    runtime.shutdown()
''')


def test_a_failed_webhook_run_is_retried_once_it_has_ended(repo):
    """The events of a run that did not finish DONE stay pending, and the task is
    queued again once that run is no longer queued or running."""
    hub, _, env = repo
    _task(hub, "alerts", 'on_webhook: squadcast\nrun: "sleep 2; exit 1"')
    _events(hub, "squadcast", 1)
    proc = subprocess.run([sys.executable, "-c", _SCHEDULER, str(hub)],
                          capture_output=True, text=True, timeout=180, env=env)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("OUT ")), None)
    assert line, proc.stderr[-2000:]
    first, during, retried, ids = json.loads(line[4:])
    assert first == ["alerts"] and during == [] and retried == ["alerts"]
    assert len(ids) == 2


_START_AND_HANG = textwrap.dedent('''
    import sys, time
    from hubzoid.workflows import markdown, runtime
    runtime.init(sys.argv[1])
    runtime.launch()
    markdown.enqueue_task("slow", "s1")
    print("QUEUED", flush=True)
    time.sleep(120)
''')

_RECOVER = textwrap.dedent('''
    import json, sys, time
    from dbos import DBOS
    from hubzoid.workflows import runtime
    runtime.init(sys.argv[1])
    runtime.launch()
    deadline = time.time() + 60
    while time.time() < deadline:
        st = DBOS.get_workflow_status("md:slow:s1")
        if st.status in ("SUCCESS", "ERROR"):
            break
        time.sleep(0.5)
    print("STATUS " + st.status + " " + json.dumps(str(st.error)))
    runtime.shutdown()
''')


@pytest.mark.skipif(os.name == "nt", reason="uses SIGKILL")
def test_interrupted_work_is_not_redone_after_restart(repo):
    """As before DBOS: a run killed mid-work is not repeated on restart; it is
    reported, and the next slot runs the task."""
    hub, _, env = repo
    _task(hub, "slow", 'run: "echo started >> starts.txt; sleep 60"\nschedule: "0 3 * * *"')
    proc = subprocess.Popen([sys.executable, "-c", _START_AND_HANG, str(hub)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    deadline = time.time() + 90
    while time.time() < deadline and not (hub / "starts.txt").exists():
        time.sleep(0.3)
    assert (hub / "starts.txt").exists(), "the task never started"
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)
    rec = subprocess.run([sys.executable, "-c", _RECOVER, str(hub)],
                         capture_output=True, text=True, timeout=180, env=env)
    status = next(ln for ln in rec.stdout.splitlines() if ln.startswith("STATUS "))
    assert "ERROR" in status and "interrupted" in status
    assert (hub / "starts.txt").read_text().count("started") == 1  # not run again


# One process per phase. argv: hub, workflow code version, phase. A fixed
# version stands in for editing workflow code or upgrading Hubzoid.
_UNDER_VERSION = textwrap.dedent('''
    import json, os, sys, time
    from dbos import DBOS
    from dbos._queue import Queue
    from hubzoid.workflows import markdown, runtime
    hub, version, phase = sys.argv[1], sys.argv[2], sys.argv[3]
    runtime._workflow_code_version = lambda *a: version
    real_enqueue = Queue.enqueue
    if phase == "enqueue-fails":
        def enqueue(self, *a, **k):
            raise RuntimeError("database is locked")
        Queue.enqueue = enqueue
    if phase == "crash-after-enqueue":
        def enqueue(self, *a, **k):
            handle = real_enqueue(self, *a, **k)
            if handle.get_workflow_id().endswith(":requeued"):
                os._exit(9)
            return handle
        Queue.enqueue = enqueue
    runtime.init(hub)
    runtime.launch()  # the sweep of runs from other code runs here
    if phase == "queue":
        markdown.enqueue_task("slow", "s1")
        markdown.enqueue_task("sync", "s1")  # waits behind "slow"
        print("QUEUED", flush=True)
        time.sleep(120)
    if phase == "recover":
        deadline = time.time() + 60
        while time.time() < deadline:
            st = DBOS.get_workflow_status("md:sync:s1:requeued")
            if st and st.status in ("SUCCESS", "ERROR"):
                break
            time.sleep(0.5)
    runs = {w.workflow_id: w.status for w in DBOS.list_workflows(workflow_id_prefix="md:sync:")}
    print("RUNS " + json.dumps(runs), flush=True)
    runtime.shutdown()
''')


@pytest.mark.skipif(os.name == "nt", reason="uses SIGKILL")
def test_queued_run_survives_a_code_change_even_if_requeue_is_interrupted(repo):
    """A markdown run still queued when the workflow code changes is queued again
    under the new code. The old run is cancelled only once its replacement is
    queued, so a failed or interrupted re-queue loses nothing: the next start
    re-queues it, and the slot runs exactly once."""
    hub, _, env = repo
    _task(hub, "slow", 'run: "echo started >> starts.txt; sleep 60"\nschedule: "0 3 * * *"')
    _task(hub, "sync", 'run: "echo x >> count.txt"\nschedule: "0 3 * * *"')

    def phase(version, name):
        proc = subprocess.run([sys.executable, "-c", _UNDER_VERSION, str(hub), version, name],
                              capture_output=True, text=True, timeout=180, env=env)
        runs = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RUNS ")), None)
        return proc.returncode, json.loads(runs[5:]) if runs else proc.stderr[-2000:]

    proc = subprocess.Popen([sys.executable, "-c", _UNDER_VERSION, str(hub), "wf-old", "queue"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    deadline = time.time() + 90
    while time.time() < deadline and not (hub / "starts.txt").exists():
        time.sleep(0.3)
    assert (hub / "starts.txt").exists(), "the task never started"
    proc.send_signal(signal.SIGKILL)  # "sync" is still queued behind "slow"
    proc.wait(timeout=30)

    _, runs = phase("wf-new", "enqueue-fails")
    assert runs == {"md:sync:s1": "ENQUEUED"}  # not cancelled without a replacement
    code, _ = phase("wf-new", "crash-after-enqueue")
    assert code == 9  # killed after the re-queue, before the cancel
    _, runs = phase("wf-new", "recover")
    assert runs == {"md:sync:s1": "CANCELLED", "md:sync:s1:requeued": "SUCCESS"}
    assert (hub / "count.txt").read_text().count("x") == 1


def test_console_lists_markdown_tasks_and_names_their_runs(repo):
    hub, _, env = repo
    _task(hub, "sync", 'run: "true"\nschedule: "0 3 * * *"')
    _task(hub, "alerts", 'on_webhook: squadcast\nrun: "true"')
    _run(hub, env, "sync", "s1")
    from hubzoid.workflows import observe

    old = {k: os.environ.get(k) for k in ("HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB")}
    os.environ.update({k: env[k] for k in old})
    try:
        cat = {r["name"]: r for r in observe.markdown_catalog(hub)}
        assert cat["md:sync"]["state"] == "scheduled" and cat["md:sync"]["next_run"]
        assert cat["md:alerts"]["state"] == "event"
        rows = observe.runs(hub, name="md:sync")
        assert [r["name"] for r in rows] == ["md:sync"]
        assert rows[0]["status"] == "SUCCESS"
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_bridge_start_runs_a_due_markdown_task(tmp_path, monkeypatch):
    """No HUBZOID_SCHEDULES needed: a hub with markdown tasks gets its engine and
    scheduler at bridge start, and a task that is due runs on it."""
    import logging
    from datetime import datetime, timedelta

    from fastapi.testclient import TestClient

    from hubzoid import scheduling as sch

    for h in list(logging.getLogger("dbos").handlers):
        logging.getLogger("dbos").removeHandler(h)
    hub = tmp_path / "hub"
    (hub / "schedule").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: hub\ndescription: d\n---\nbody")
    _task(hub, "ping", 'run: "echo pong > ping.txt"\nschedule: "0 3 * * *"')
    sch.ScheduleState(hub).record_fired("ping", datetime.now() - timedelta(days=2), result="done")
    for k in ("HUBZOID_SCHEDULES", "HUBZOID_GATEWAY", "HUBZOID_DISABLE_SCHEDULE",
              "HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-used")
    monkeypatch.setenv("BRIDGE_API_KEYS", "k-md")
    from hubzoid.server import build_app

    with TestClient(build_app()):
        deadline = time.time() + 60
        while time.time() < deadline and not (hub / "ping.txt").exists():
            time.sleep(0.5)
        deadline = time.time() + 30
        while time.time() < deadline and sch.ScheduleState(hub).get("ping").get("last_result") != "done":
            time.sleep(0.5)
    assert (hub / "ping.txt").read_text().strip() == "pong"
    assert sch.ScheduleState(hub).get("ping")["last_result"] == "done"


def test_list_form_run_task(repo):
    """`run: ["python", "jobs/x.py"]` (an argv list, as deployed hubs use)."""
    hub, _, env = repo
    (hub / "jobs").mkdir()
    (hub / "jobs" / "digest.py").write_text("open('digest.txt', 'w').write('sent')\n")
    _task(hub, "digest", f'schedule: "0 6 * * *"\nrun: ["{sys.executable}", "jobs/digest.py"]\ntimeout: 600')
    out, _ = _run(hub, env, "digest", "20260926T0600")
    assert out["result"] == "done"
    assert (hub / "digest.txt").read_text() == "sent"


def test_run_ids_name_their_task_even_when_requeued():
    from hubzoid.workflows.markdown import task_name_from_id

    assert task_name_from_id("md:sync:20260925T0300") == "sync"
    assert task_name_from_id("md:sync:20260925T0300:requeued") == "sync"
    assert task_name_from_id("md:sync:20260925T0300:requeued:requeued") == "sync"
    assert task_name_from_id("md:alerts:events-20260925T0300-abcdef0123456789") == "alerts"
    assert task_name_from_id("wf-123") is None
