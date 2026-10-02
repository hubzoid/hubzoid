"""Ephemeral PostgreSQL acceptance; never connects to an existing deployment."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import subprocess
import threading

import pytest
from sqlalchemy import create_engine, text

from hubzoid.access.store import GrantStore, LastAdminError, RevisionConflict, USE_HUB


def test_postgres_concurrent_admin_revocation_and_policy_refresh(postgres_url):
    engines = [create_engine(postgres_url) for _ in range(2)]
    try:
        a, b = [GrantStore(e) for e in engines]
        a.bootstrap(["first", "second"])
        barrier = threading.Barrier(2)

        def revoke(store, subject):
            barrier.wait()
            try:
                store.revoke(subject, "*", "manage_access")
                return "removed"
            except LastAdminError:
                return "protected"

        with ThreadPoolExecutor(2) as pool:
            futures = [
                pool.submit(revoke, a, "first"),
                pool.submit(revoke, b, "second"),
            ]
            assert sorted(f.result(timeout=15) for f in futures) == [
                "protected",
                "removed",
            ]
        assert len(a.list_grants("*")) == 1
        a.grant("person", "finance", "ledger")
        assert b.can("person", "finance", "ledger")
        a.revoke("person", "finance", "use_hub")
        assert not b.can("person", "finance", "ledger")
    finally:
        for e in engines:
            e.dispose()


@pytest.mark.slow
def test_postgres_dbos_recovers_after_process_exit(postgres_url, tmp_path):
    import os
    import sys
    import time

    workflow = tmp_path / "workflows" / "recover"
    workflow.mkdir(parents=True)
    marker, gate, effects = [
        tmp_path / name for name in ("checkpoint", "resume", "effects")
    ]
    (workflow / "main.py").write_text(f"""from pathlib import Path
import time
from hubzoid import workflow, step
@step()
def first():
    with open({str(effects)!r}, 'a') as f: f.write('once\\n')
    return 'checkpointed'
@step()
def wait_for_resume():
    Path({str(marker)!r}).touch()
    while not Path({str(gate)!r}).exists(): time.sleep(0.05)
@workflow()
def recover():
    result = first()
    wait_for_resume()
    return result
""")
    script = """import sys, time
from hubzoid.workflows import runtime
runtime.init(sys.argv[1])
runtime.load_workflows(sys.argv[1])
runtime.launch()
h = runtime.start('recover', scheduled_at='2026-01-01T00:00:00+00:00')
assert h.get_result() == 'checkpointed'
runtime.shutdown()
print('RECOVERED', flush=True)
"""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("HUBZOID_DEPLOYMENT", "DATABASE_URL")
    }
    env["HUBZOID_DBOS_DB"] = postgres_url
    env["HUBZOID_OPERATIONAL_DB"] = postgres_url
    first = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        until = time.monotonic() + 30
        while not marker.exists():
            if first.poll() is not None:
                out, err = first.communicate()
                raise AssertionError(out + err)
            assert time.monotonic() < until, "workflow did not reach checkpoint"
            time.sleep(0.05)
        first.kill()
        first.communicate(timeout=10)
        gate.touch()
        # A dropped session frees the advisory lock at once, but the killed
        # owner's lease still runs: no new owner starts until it lapses.
        early = subprocess.run([sys.executable, "-c", script, str(tmp_path)], env=env,
                               capture_output=True, text=True, timeout=45)
        assert early.returncode != 0 and "lease ends in" in early.stderr
        with create_engine(postgres_url).begin() as conn:
            conn.execute(text("UPDATE hz_workflow_owner SET expires=0 WHERE hub=:h"),
                         {"h": tmp_path.name})
        second = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert second.returncode == 0, second.stderr
        assert "RECOVERED" in second.stdout
        assert effects.read_text() == "once\n"
    finally:
        if first.poll() is None:
            first.kill()
        first.communicate()


_LOSS_SCRIPT = """import asyncio, os, sys, time
from pathlib import Path
from sqlalchemy import text
from hubzoid.workflows import boot, runtime
hub = Path(sys.argv[1])
boot.OWNER_RETRY_SECONDS = 1

def statuses(ids):
    from dbos import DBOS
    return {w.workflow_id: w.status for w in DBOS.list_workflows(workflow_ids=ids, load_input=False, load_output=False)}

async def main():
    sup = await boot.start(hub)
    first = sup.dispatcher
    pid = first.owner.lock.execute(text('SELECT pg_backend_pid()')).scalar()
    first.owner.lock.commit()
    ids = [(await asyncio.to_thread(runtime.start, 'slow')).get_workflow_id() for _ in range(5)]
    (hub / 'ids').write_text('\\n'.join(ids))
    print('PID', pid, flush=True)
    await asyncio.wait_for(first.lost.wait(), 30)
    print('LOST', flush=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if sup.dispatcher is not None and runtime._LAUNCHED:
            done = await asyncio.to_thread(statuses, ids)
            if all(v == 'SUCCESS' for v in done.values()) and len(done) == 5:
                print('ALL_DONE', flush=True)
                break
            assert 'ERROR' not in done.values(), done
        await asyncio.sleep(0.2)
    else:
        print('TIMED_OUT', flush=True)
    await sup.stop()

asyncio.run(main())
"""


@pytest.mark.slow
def test_postgres_owner_loss_stops_claiming_and_keeps_work_recoverable(postgres_url, tmp_path):
    """The lock session dies mid-run: no run is failed at an ownership guard,
    the backlog is not drained, and the bridge regains ownership and finishes
    every run once. The process (chat) stays up throughout."""
    import os
    import sys
    import time

    effects, started, gate = (tmp_path / n for n in ("effects", "started", "gate"))
    workflow = tmp_path / "workflows" / "slow"
    workflow.mkdir(parents=True)
    (workflow / "main.py").write_text(f"""import time
from pathlib import Path
from hubzoid import workflow, step
@step()
def mark():
    with open({str(effects)!r}, 'a') as f: f.write('done\\n')
@workflow(concurrency=1)
def slow():
    Path({str(started)!r}).touch()
    while not Path({str(gate)!r}).exists(): time.sleep(0.05)
    mark()
""")
    env = {k: v for k, v in os.environ.items() if k not in ("HUBZOID_DEPLOYMENT", "DATABASE_URL")}
    env.update(HUBZOID_DBOS_DB=postgres_url, HUBZOID_OPERATIONAL_DB=postgres_url,
               HUBZOID_SCHEDULES="1")
    proc = subprocess.Popen([sys.executable, "-c", _LOSS_SCRIPT, str(tmp_path)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        pid = None
        until = time.monotonic() + 60
        while pid is None or not started.exists():
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < until, "first run did not start"
            if pid is None:
                line = proc.stdout.readline()
                if line.startswith("PID "):
                    pid = int(line.split()[1])
            else:
                time.sleep(0.05)
        with create_engine(postgres_url).begin() as conn:
            assert conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid}).scalar()
        gate.touch()   # the in-flight run now reaches its guarded step
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, out + err[-4000:]
        assert "LOST" in out and "ALL_DONE" in out, out + err[-4000:]
        assert effects.read_text() == "done\n" * 5
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_postgres_access_history_retains_timestamp_precision(postgres_url):
    import time

    engine = create_engine(postgres_url)
    try:
        store = GrantStore(engine)
        before = time.time()
        store.grant("timestamp-test", "precision", "use_hub")
        after = time.time()
        row = next(
            r for r in store.read_access_audit() if r["subject"] == "timestamp-test"
        )
        assert before <= row["ts"] <= after
    finally:
        engine.dispose()


@pytest.mark.slow
def test_postgres_same_named_workflows_are_hub_scoped(postgres_url, tmp_path):
    import os
    import sys

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("DATABASE_URL", "HUBZOID_DEPLOYMENT")
    }
    env.update(HUBZOID_DBOS_DB=postgres_url, HUBZOID_OPERATIONAL_DB=postgres_url)
    hubs = [tmp_path / name for name in ("team.alpha", "team-alpha")]
    for hub in hubs:
        folder = hub / "workflows" / "daily"
        folder.mkdir(parents=True)
        (folder / "main.py").write_text(
            f"from hubzoid import workflow\n@workflow()\ndef daily(): return {hub.name!r}\n"
        )
        script = """import sys
from pathlib import Path
from hubzoid.workflows import runtime, observe
hub=Path(sys.argv[1])
runtime.init(hub); runtime.load_workflows(hub); runtime.launch()
h=runtime.start('daily', scheduled_at='2026-01-01T00:00:00+00:00')
assert h.get_result() == hub.name
rows=observe.runs(hub,name='daily',viewer='admin@localhost')
assert len(rows) == 1, rows
assert rows[0]['output'] == hub.name, rows
runtime.shutdown()
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(hub)],
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr


def test_postgres_two_simultaneous_applies_one_wins(postgres_url):
    a, b = GrantStore(create_engine(postgres_url)), GrantStore(create_engine(postgres_url))
    rev = a.revision()
    barrier = threading.Barrier(2)

    def apply(store, subject):
        barrier.wait()
        try:
            store.apply_changes(subject, "finance", [("grant", "ledger")],
                                expected_revision=rev, actor="admin")
            return "ok"
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(apply, a, "alice")
        f2 = ex.submit(apply, b, "bob")
        results = sorted([f1.result(), f2.result()])
    assert results == ["conflict", "ok"], results


def test_postgres_snapshot_reads_stay_consistent(postgres_url):
    writer = GrantStore(create_engine(postgres_url))
    reader = GrantStore(create_engine(postgres_url))
    rev0, grants0 = reader.access_snapshot()
    base = len(grants0)
    N = 30
    stop = threading.Event()
    seen = []

    def read_loop():
        while not stop.is_set():
            rev, grants = reader.access_snapshot()
            seen.append((rev - rev0, len(grants) - base))

    t = threading.Thread(target=read_loop)
    t.start()
    try:
        for i in range(N):
            writer.grant(f"pguser{i}", "finance", USE_HUB)
    finally:
        stop.set()
        t.join()
    for _ in range(5):
        rev, grants = reader.access_snapshot()
        seen.append((rev - rev0, len(grants) - base))
    for d_rev, d_grants in seen:
        assert d_rev == d_grants, f"inconsistent PG snapshot: rev+{d_rev} grants+{d_grants}"


def _seed_dbos_pg(url, app, rows):
    """Seed workflow_status rows into a shared-Postgres DBOS system DB (schema
    ``dbos``), exercised through the real ``list_workflows`` read path."""
    from dbos import DBOSClient

    client = DBOSClient(system_database_url=url, application_name=app, retry_connection_errors=False)
    client._sys_db.run_migrations()
    with client._sys_db.engine.begin() as conn:
        for r in rows:
            conn.execute(
                text(
                    "INSERT INTO dbos.workflow_status "
                    "(workflow_uuid, status, name, application_name, created_at, "
                    " updated_at, recovery_attempts) "
                    "VALUES (:id, :status, :name, :app, :created, :created, 0)"
                ),
                {"id": r["id"], "status": r["status"], "name": r["name"], "app": app, "created": r["created_ms"]},
            )
    client.destroy()


def test_equal_timestamp_pagination_postgres(postgres_url, tmp_path, monkeypatch):
    """Review #1, PostgreSQL: on a shared DBOS system DB, ten runs sharing one
    created_at must paginate deterministically (id tie-break, boundary-group
    completion) — concatenated pages equal the full result, no skips/duplicates."""
    import hubzoid.db as db
    from hubzoid.workflows import observe
    from hubzoid.workflows.runtime import _app_name

    d = tmp_path / "finance"
    d.mkdir()
    monkeypatch.setattr(db, "dbos_url", lambda hub_dir, env=None: postgres_url)
    same = 1_756_990_800_000  # fixed epoch ms shared by every run
    _seed_dbos_pg(
        postgres_url,
        _app_name("finance"),
        [{"id": c, "status": "SUCCESS", "name": "w", "created_ms": same} for c in "jihgfedcba"],
    )
    hubs = [{"key": "finance", "name": "Finance", "path": str(d.resolve())}]
    full = [r["id"] for r in observe.runs_across(hubs, limit=50)["runs"]]
    assert full == list("jihgfedcba")
    walked = []
    for off in range(0, 10, 2):
        page = observe.runs_across(hubs, limit=2, offset=off)
        walked += [r["id"] for r in page["runs"]]
        assert page["has_more"] is (off + 2 < 10)
    assert walked == full
    assert len(walked) == len(set(walked)) == 10


def _seed_owui_sqlite(path, users, grants):
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE user (id TEXT PRIMARY KEY, email TEXT, role TEXT)")
    con.execute("CREATE TABLE model (id TEXT PRIMARY KEY, user_id TEXT, is_active INTEGER, access_control TEXT)")
    con.execute('CREATE TABLE "group" (id TEXT PRIMARY KEY, name TEXT)')
    con.execute("CREATE TABLE group_member (group_id TEXT, user_id TEXT)")
    con.execute("CREATE TABLE access_grant (resource_type TEXT, resource_id TEXT, principal_type TEXT, principal_id TEXT, permission TEXT)")
    for u in users:
        con.execute("INSERT INTO user VALUES (?,?,?)", u)
    con.execute("INSERT INTO model VALUES ('m1','uadmin',1,NULL)")
    for g in grants:
        con.execute("INSERT INTO access_grant VALUES (?,?,?,?,?)", g)
    con.commit()
    con.close()


