"""Shared test fixtures."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess

import pytest

# LiteLLM downloads its model price list at import unless told to use the copy
# it ships. That remote list changes under us (October 2026 it stopped pricing
# openrouter/anthropic/claude-haiku-4.5), so tests read the shipped copy.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# 2.0 asks administrators to set up two-step sign-in by default. Tests that are
# about something else sign in with a password alone; the MFA tests turn the
# policy on themselves (tests/test_auth_mfa.py).
os.environ.setdefault("HUBZOID_MFA_REQUIRED", "none")

# Every bridge takes a weekly backup by default. Tests that build an app are
# about something else; tests/test_backup_verify.py runs the schedule itself.
os.environ.setdefault("HUBZOID_BACKUPS", "off")

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


@pytest.fixture
def fresh_postgres_url(postgres_url):
    """A new, empty database on the session's server: for tests whose result
    depends on rows other tests leave behind (who the org admins are)."""
    import uuid

    from sqlalchemy import create_engine, text

    name = "hz_" + uuid.uuid4().hex[:12]
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    admin.dispose()
    return postgres_url.rsplit("/", 1)[0] + "/" + name
