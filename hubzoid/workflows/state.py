# Hubzoid workflows. MIT licensed like the rest of the repository.
"""Durable per-workflow key/value state in the one hub-owned database.

`hub.state` is dict-like memory that survives across runs (e.g. "last commit
reviewed"). It is keyed **(hub, workflow, key)** so two workflows in the same hub
never clash on the same key. Values are JSON. Same thin SQLAlchemy-Core path as
`db.py`, across SQLite and Postgres.

Durability semantics (read this): a `hub.state[...] = v` write is its own DB
commit — durable the instant it returns. It is NOT a DBOS-checkpointed step, so
across a crash + replay a **read-modify-write is not atomic**: a naive
``state["n"] = state.get("n", 0) + 1`` can double-count if the run crashes after
the write but before the workflow completes and is later replayed. Use
**idempotent** patterns instead — the canonical one is a per-item marker
(``state[f"done:{id}"] = sha``), which is what makes a re-run skip already-done
work. Do not use `hub.state` as a transactional counter across steps.
"""
from __future__ import annotations

import json
import threading

from sqlalchemy import text
from sqlalchemy.engine import Engine

_MISSING = object()
_ensured: set[int] = set()
_lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS hz_workflow_kv (
    hub      TEXT NOT NULL,
    workflow TEXT NOT NULL,
    k        TEXT NOT NULL,
    v        TEXT,
    PRIMARY KEY (hub, workflow, k)
)
"""


def ensure_state_table(engine: Engine) -> None:
    key = id(engine)
    if key in _ensured:
        return
    with _lock:
        if key in _ensured:
            return
        with engine.begin() as conn:
            conn.execute(text(_DDL))
        _ensured.add(key)


class WorkflowState:
    """Dict-like durable state for one (hub, workflow). Reads/writes go straight
    to the DB so a restart resumes exactly where it left off."""

    def __init__(self, engine: Engine, hub: str, workflow: str):
        ensure_state_table(engine)
        self._engine = engine
        self._hub = hub
        self._workflow = workflow

    def get(self, key: str, default=None):
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT v FROM hz_workflow_kv "
                    "WHERE hub=:h AND workflow=:w AND k=:k"
                ),
                {"h": self._hub, "w": self._workflow, "k": key},
            ).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else default

    def __getitem__(self, key: str):
        v = self.get(key, _MISSING)
        if v is _MISSING:
            raise KeyError(key)
        return v

    def __setitem__(self, key: str, value) -> None:
        payload = json.dumps(value)
        dialect = self._engine.dialect.name
        with self._engine.begin() as conn:
            if dialect == "sqlite":
                conn.execute(
                    text(
                        "INSERT INTO hz_workflow_kv (hub, workflow, k, v) "
                        "VALUES (:h, :w, :k, :v) "
                        "ON CONFLICT (hub, workflow, k) DO UPDATE SET v=excluded.v"
                    ),
                    {"h": self._hub, "w": self._workflow, "k": key, "v": payload},
                )
            else:
                conn.execute(
                    text(
                        "INSERT INTO hz_workflow_kv (hub, workflow, k, v) "
                        "VALUES (:h, :w, :k, :v) "
                        "ON CONFLICT (hub, workflow, k) DO UPDATE SET v=excluded.v"
                    ),
                    {"h": self._hub, "w": self._workflow, "k": key, "v": payload},
                )

    def __contains__(self, key: str) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def __delitem__(self, key: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM hz_workflow_kv WHERE hub=:h AND workflow=:w AND k=:k"
                ),
                {"h": self._hub, "w": self._workflow, "k": key},
            )
