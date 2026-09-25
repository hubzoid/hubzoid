"""Shared test fixtures."""
from __future__ import annotations

import shutil
import socket
import subprocess

import pytest


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
