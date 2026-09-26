"""Engine settings for Hubzoid's own databases."""
from __future__ import annotations

from sqlalchemy import text

from hubzoid import db


def test_sqlite_engines_wait_for_a_writers_lock(tmp_path):
    """Several gateway bridges write one SQLite file; a short lock wait would
    turn ordinary contention into refused requests."""
    eng = db._engine_for_url(f"sqlite:///{tmp_path / 'op.db'}")
    try:
        with eng.connect() as conn:
            waited_ms = conn.execute(text("PRAGMA busy_timeout")).scalar()
        assert waited_ms == db.SQLITE_LOCK_WAIT_SECONDS * 1000
    finally:
        eng.dispose()
