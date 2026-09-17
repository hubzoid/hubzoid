"""Ephemeral PostgreSQL acceptance; never connects to an existing deployment."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import shutil
import socket
import subprocess
import threading

import pytest
from sqlalchemy import create_engine, text

from hubzoid.access.store import GrantStore, LastAdminError


@pytest.fixture(scope="module")
def postgres_url(tmp_path_factory):
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("local PostgreSQL binaries are not installed")
    root = tmp_path_factory.mktemp("hubzoid-postgres")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.run(
        [
            initdb,
            "-D",
            str(root / "data"),
            "-U",
            "hz_test",
            "-A",
            "trust",
            "--no-locale",
            "--encoding=UTF8",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            pg_ctl,
            "-D",
            str(root / "data"),
            "-l",
            str(root / "server.log"),
            "-o",
            f"-h 127.0.0.1 -p {port} -k ''",
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        yield f"postgresql+psycopg://hz_test@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(root / "data"), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
            text=True,
        )


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


def test_postgres_migration_atomicity_and_hub_isolation(postgres_url):
    engine = create_engine(postgres_url)
    try:
        gs = GrantStore(engine)
        gs.grant("retained", "other", "use_hub")
        barrier = threading.Barrier(2)

        def cutover(hub):
            barrier.wait()
            gs.apply_migration([(hub + "@example.com", hub, "ledger")], [], [hub])

        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(cutover, hub) for hub in ("alpha", "beta")]
            for f in futures:
                f.result(timeout=15)
        assert gs.can("alpha@example.com", "alpha", "ledger")
        assert not gs.can("alpha@example.com", "beta", "ledger")
        assert gs.can("retained", "other", "use_hub")
        snapshot = gs.snapshot(["alpha"])
        gs.restore(snapshot, actor="test")
        assert gs.snapshot(["alpha"]) == snapshot
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hz_access_audit")).scalar() > 0
            )
    finally:
        engine.dispose()


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


def test_postgres_same_hub_cutovers_do_not_merge_plans(postgres_url):
    engine = create_engine(postgres_url)
    try:
        store = GrantStore(engine)
        barrier = threading.Barrier(2)

        def cutover(subject):
            barrier.wait()
            store.apply_migration(
                [(subject, "concurrent-hub", "ledger")], [], ["concurrent-hub"]
            )

        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(cutover, s) for s in ("migration-a", "migration-b")]
            for future in futures:
                future.result(timeout=15)
        assert len({s for s, _, _ in store.list_grants("concurrent-hub")}) == 1
    finally:
        engine.dispose()


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
rows=observe.runs(hub,name='daily')
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
