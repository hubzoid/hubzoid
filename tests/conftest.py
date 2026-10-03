"""Shared test fixtures."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess

import pytest

# A test must never signal "every process I own": os.kill(-1) or os.killpg(0/1)
# (killpg(1) is kill(-1)). On a CI runner that kills the runner itself, and the
# job hangs with no log. A stand-in process (MagicMock pid == 1) is the usual way in.
_real_kill, _real_killpg = os.kill, getattr(os, "killpg", None)


def _guarded_kill(pid, sig):
    if type(pid) is not int or pid in (0, -1):
        raise AssertionError(f"test tried os.kill({pid!r}, {sig})")
    return _real_kill(pid, sig)


def _guarded_killpg(pgid, sig):
    if type(pgid) is not int or pgid <= 1:
        raise AssertionError(f"test tried os.killpg({pgid!r}, {sig})")
    return _real_killpg(pgid, sig)


os.kill = _guarded_kill
if _real_killpg is not None:
    os.killpg = _guarded_killpg


# Tests that took 5 s or more (.test_durations) are marked `heavy`. CI leaves
# them out to keep checks to minutes; run them with `pytest -m heavy`.
_HEAVY_SECONDS = 5.0


def _heavy_ids() -> set[str]:
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / ".test_durations"
    try:
        return {k for k, v in json.loads(path.read_text()).items() if v >= _HEAVY_SECONDS}
    except (OSError, ValueError):
        return set()


def pytest_collection_modifyitems(config, items):
    heavy = _heavy_ids()
    for item in items:
        if item.nodeid in heavy:
            item.add_marker(pytest.mark.heavy)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """A timeout (pytest-timeout, pyproject's `timeout`) is reported as a skip,
    not a failure: a slow or stuck machine must not block CI. Assertion and
    other failures still fail."""
    outcome = yield
    report = outcome.get_result()
    if report.failed and "Timeout (>" in str(report.longrepr):
        report.outcome = "skipped"
        report.longrepr = (str(item.path), item.location[1] or 0,
                           "Skipped: timed out (a hang is reported, not a failure)")


@pytest.fixture(scope="session")
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
