# Hubzoid workflows. Apache-2.0 licensed like the rest of the repository.
"""Durable per-workflow key/value state in the one hub-owned database.

`hub.state` is dict-like memory that survives across runs (e.g. "last commit
reviewed"). It is keyed **(hub, workflow, owner, key)**: `owner` is the account
the run acts as, so the same workflow run for two people never shares state, and
two workflows in the same hub never clash on a key. `hub.shared_state` is the
explicit exception (owner `*`) for data that is not about any one person. Rows
written before workflows ran as people have owner '' and are adopted by the
first person who runs that workflow afterwards (`adopt_legacy`). Values are
JSON. Same thin SQLAlchemy-Core path as `db.py`, across SQLite and Postgres.

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

from sqlalchemy import text
from sqlalchemy.engine import Engine

_MISSING = object()
SHARED = "*"   # the owner of hub.shared_state


def ensure_state_table(engine: Engine) -> None:
    """`hz_workflow_kv` lives in the operational store's versioned schema."""
    from ..migrations import upgrade

    upgrade(engine, "operational")


class WorkflowState:
    """Dict-like durable state for one (hub, workflow, owner). Reads/writes go
    straight to the DB so a restart resumes exactly where it left off."""

    def __init__(self, engine: Engine, hub: str, workflow: str, owner: str = ""):
        ensure_state_table(engine)
        self._engine = engine
        self._hub = hub
        self._workflow = workflow
        self._owner = owner

    def _key(self, key: str) -> dict:
        return {"h": self._hub, "w": self._workflow, "o": self._owner, "k": key}

    def get(self, key: str, default=None):
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT v FROM hz_workflow_kv "
                    "WHERE hub=:h AND workflow=:w AND owner=:o AND k=:k"
                ),
                self._key(key),
            ).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else default

    def __getitem__(self, key: str):
        v = self.get(key, _MISSING)
        if v is _MISSING:
            raise KeyError(key)
        return v

    def __setitem__(self, key: str, value) -> None:
        payload = json.dumps(value)
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO hz_workflow_kv (hub, workflow, owner, k, v) "
                    "VALUES (:h, :w, :o, :k, :v) "
                    "ON CONFLICT (hub, workflow, owner, k) DO UPDATE SET v=excluded.v"
                ),
                {**self._key(key), "v": payload},
            )

    def __contains__(self, key: str) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def __delitem__(self, key: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM hz_workflow_kv "
                    "WHERE hub=:h AND workflow=:w AND owner=:o AND k=:k"
                ),
                self._key(key),
            )


def adopt_legacy(engine: Engine, hub: str, workflow: str, owner: str) -> int:
    """Give a workflow's pre-identity state (owner '') to `owner`, once.

    Only the first person to run the workflow afterwards adopts it: if `owner`
    already has any state for this workflow, nothing moves. Returns the number
    of rows adopted."""
    ensure_state_table(engine)
    if not owner or owner == SHARED:
        return 0
    params = {"h": hub, "w": workflow, "o": owner}
    with engine.begin() as conn:
        has = conn.execute(
            text("SELECT 1 FROM hz_workflow_kv WHERE hub=:h AND workflow=:w AND owner=:o "
                 "LIMIT 1"), params).fetchone()
        if has:
            return 0
        result = conn.execute(
            text("UPDATE hz_workflow_kv SET owner=:o "
                 "WHERE hub=:h AND workflow=:w AND owner=''"), params)
        return result.rowcount or 0
