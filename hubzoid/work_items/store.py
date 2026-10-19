"""Rows for work items and operations, in the shared operational store.

Every change is a compare-and-set on `revision`, so two writers can never both
win. Admission takes a short write lock (SQLite `BEGIN IMMEDIATE`, a PostgreSQL
advisory lock) and the unique key `(hub, logical_key, generation)` backs it up.
No transaction is held open across a network call.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import text

from .. import db, migrations
from .errors import not_found, revision_conflict

ACTIVE = ("accepted", "running", "waiting", "needs_input")
TERMINAL = ("completed", "stopped", "failed", "cancelled")
ITEM_STATES = ACTIVE + TERMINAL
OP_STATES = ("pending", "awaiting_approval", "sending", "succeeded", "failed_before_send",
             "rejected", "ambiguous", "resolved_done", "abandoned")
OP_UNRESOLVED = ("sending", "ambiguous")

_ITEM_JSON = ("route_json", "input_json", "result_json", "origin_json", "counters_json", "inbox_json")
_OP_JSON = ("target_json", "approval_json", "attempts_json", "result_json", "evidence_json",
            "resolution_json")


def engine(hub_dir):
    eng = db.operational_engine(Path(hub_dir))
    migrations.upgrade(eng, "operational")
    return eng


def now() -> float:
    return time.time()


def new_id() -> str:
    return str(uuid.uuid4())


def dumps(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _decode(row, json_cols) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col in json_cols:
        if col in out and out[col] is not None:
            out[col] = json.loads(out[col])
    return out


@contextmanager
def locked(hub_dir, name: str):
    """A short write transaction serialized on `name` across processes."""
    eng = engine(hub_dir)
    with eng.connect() as conn:
        if eng.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            conn.begin()
            key = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


# -- work items -------------------------------------------------------------

def get_item(hub_dir, item_id: str, *, conn=None) -> dict | None:
    sql = text("SELECT * FROM hz_work_items WHERE id=:i")
    if conn is not None:
        row = conn.execute(sql, {"i": item_id}).mappings().first()
    else:
        with engine(hub_dir).connect() as c:
            row = c.execute(sql, {"i": item_id}).mappings().first()
    return _decode(row, _ITEM_JSON)


def require_item(hub_dir, item_id: str, hub: str | None = None) -> dict:
    item = get_item(hub_dir, item_id)
    if item is None or (hub is not None and item["hub"] != hub):
        raise not_found()
    return item


def latest_for_key(conn, hub: str, logical_key: str) -> dict | None:
    row = conn.execute(text(
        "SELECT * FROM hz_work_items WHERE hub=:h AND logical_key=:k "
        "ORDER BY generation DESC LIMIT 1"), {"h": hub, "k": logical_key}).mappings().first()
    return _decode(row, _ITEM_JSON)


def count_active(conn, hub: str) -> int:
    return conn.execute(text(
        "SELECT count(*) FROM hz_work_items WHERE hub=:h AND state IN "
        "('accepted','running','waiting','needs_input')"), {"h": hub}).scalar_one()


def insert_item(conn, **fields) -> dict:
    t = now()
    row = dict(fields, revision=1, created=t, updated=t)
    for col in _ITEM_JSON:
        if col in row and row[col] is not None and not isinstance(row[col], str):
            row[col] = dumps(row[col])
    cols = ", ".join(row)
    conn.execute(text(f"INSERT INTO hz_work_items ({cols}) VALUES ({', '.join(':' + c for c in row)})"), row)
    return _decode(conn.execute(text("SELECT * FROM hz_work_items WHERE id=:i"),
                                {"i": row["id"]}).mappings().first(), _ITEM_JSON)


def update_item(hub_dir, item_id: str, *, expected_revision: int | None = None,
                expect_state: tuple | None = None, expect_workflow_id: str | None = None,
                conn=None, **fields) -> dict:
    """Change an item with a compare-and-set. `expected_revision`,
    `expect_state` and `expect_workflow_id` (the execution that owns it) guard
    against a concurrent change. Returns the new row."""
    fields = {k: (dumps(v) if k in _ITEM_JSON and v is not None and not isinstance(v, str) else v)
              for k, v in fields.items()}
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    where = "id=:_id"
    params = dict(fields, _id=item_id, _t=now())
    if expected_revision is not None:
        where += " AND revision=:_rev"
        params["_rev"] = expected_revision
    if expect_state:
        where += " AND state IN (" + ", ".join(f":_s{i}" for i in range(len(expect_state))) + ")"
        params.update({f"_s{i}": s for i, s in enumerate(expect_state)})
    if expect_workflow_id is not None:
        where += " AND workflow_id=:_wid"
        params["_wid"] = expect_workflow_id
    sql = text(f"UPDATE hz_work_items SET {sets + ', ' if sets else ''}revision=revision+1, "
               f"updated=:_t WHERE {where}")

    def run(c):
        changed = c.execute(sql, params).rowcount
        current = get_item(hub_dir, item_id, conn=c)
        if current is None:
            raise not_found()
        if not changed:
            raise revision_conflict(current["revision"])
        return current

    if conn is not None:
        return run(conn)
    with engine(hub_dir).begin() as c:
        return run(c)


def list_items(hub_dir, hub: str, *, state: str | None = None, workflow: str | None = None,
               requester: str | None = None, before: float | None = None, limit: int = 50) -> list[dict]:
    where, params = ["hub=:h"], {"h": hub, "n": max(1, min(int(limit), 100))}
    if state:
        where.append("state=:s")
        params["s"] = state
    if workflow:
        where.append("workflow=:w")
        params["w"] = workflow
    if requester is not None:
        where.append("requester=:r")
        params["r"] = requester
    if before is not None:
        where.append("created<:b")
        params["b"] = before
    with engine(hub_dir).connect() as c:
        rows = c.execute(text(
            f"SELECT * FROM hz_work_items WHERE {' AND '.join(where)} "
            "ORDER BY created DESC LIMIT :n"), params).mappings().all()
    return [_decode(r, _ITEM_JSON) for r in rows]


# -- operations -------------------------------------------------------------

def get_op(hub_dir, op_id: str, *, conn=None) -> dict | None:
    sql = text("SELECT * FROM hz_operations WHERE id=:i")
    if conn is not None:
        row = conn.execute(sql, {"i": op_id}).mappings().first()
    else:
        with engine(hub_dir).connect() as c:
            row = c.execute(sql, {"i": op_id}).mappings().first()
    return _decode(row, _OP_JSON)


def op_by_key(hub_dir, logical_key: str) -> dict | None:
    with engine(hub_dir).connect() as c:
        row = c.execute(text("SELECT * FROM hz_operations WHERE logical_key=:k"),
                        {"k": logical_key}).mappings().first()
    return _decode(row, _OP_JSON)


def insert_op(hub_dir, **fields) -> tuple[dict, bool]:
    """Insert an operation intent, or return the one already recorded under
    the same logical key. Returns (row, created)."""
    from sqlalchemy.exc import IntegrityError

    t = now()
    row = dict(fields, revision=1, created=t, updated=t)
    for col in _OP_JSON:
        if col in row and row[col] is not None and not isinstance(row[col], str):
            row[col] = dumps(row[col])
    cols = ", ".join(row)
    try:
        with engine(hub_dir).begin() as c:
            c.execute(text(f"INSERT INTO hz_operations ({cols}) VALUES ({', '.join(':' + k for k in row)})"), row)
    except IntegrityError:
        existing = op_by_key(hub_dir, fields["logical_key"])
        if existing is None:
            raise
        return existing, False
    return get_op(hub_dir, row["id"]), True


def update_op(hub_dir, op_id: str, *, expected_revision: int | None = None,
              expect_state: tuple | None = None, **fields) -> dict:
    fields = {k: (dumps(v) if k in _OP_JSON and v is not None and not isinstance(v, str) else v)
              for k, v in fields.items()}
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    where = "id=:_id"
    params = dict(fields, _id=op_id, _t=now())
    if expected_revision is not None:
        where += " AND revision=:_rev"
        params["_rev"] = expected_revision
    if expect_state:
        where += " AND state IN (" + ", ".join(f":_s{i}" for i in range(len(expect_state))) + ")"
        params.update({f"_s{i}": s for i, s in enumerate(expect_state)})
    with engine(hub_dir).begin() as c:
        changed = c.execute(text(
            f"UPDATE hz_operations SET {sets + ', ' if sets else ''}revision=revision+1, updated=:_t "
            f"WHERE {where}"), params).rowcount
        current = get_op(hub_dir, op_id, conn=c)
    if current is None:
        raise not_found("operation")
    if not changed:
        raise revision_conflict(current["revision"])
    return current


def list_ops(hub_dir, hub: str, *, item_id: str | None = None, state: str | None = None,
             limit: int = 100) -> list[dict]:
    where, params = ["hub=:h"], {"h": hub, "n": max(1, min(int(limit), 100))}
    if item_id:
        where.append("item_id=:i")
        params["i"] = item_id
    if state:
        where.append("state=:s")
        params["s"] = state
    with engine(hub_dir).connect() as c:
        rows = c.execute(text(
            f"SELECT * FROM hz_operations WHERE {' AND '.join(where)} ORDER BY created DESC LIMIT :n"),
            params).mappings().all()
    return [_decode(r, _OP_JSON) for r in rows]


# -- idempotency keys -------------------------------------------------------

def idempotent(hub_dir, caller: str, route: str, key: str | None, body, produce):
    """Run `produce()` once per (caller, route, key). The key is reserved
    before anything is created, so two concurrent requests with one key create
    one thing. A repeat with the same body returns the first (status,
    response). The same key with a different body is refused. Without a key,
    `produce()` simply runs."""
    from sqlalchemy.exc import IntegrityError

    from .errors import WorkItemError

    if not key:
        return produce()
    if len(key) > 255:
        raise WorkItemError("invalid_request", "Idempotency-Key is at most 255 characters.")
    digest = "sha256:" + hashlib.sha256(dumps(body).encode()).hexdigest()
    params = {"c": caller, "r": route, "k": key}
    try:
        with engine(hub_dir).begin() as c:
            c.execute(text("INSERT INTO hz_idempotency (caller, route, key, digest, status_code, "
                           "response_json, created) VALUES (:c, :r, :k, :d, 0, '{}', :t)"),
                      dict(params, d=digest, t=now()))
    except IntegrityError:
        with engine(hub_dir).connect() as c:
            row = c.execute(text("SELECT digest, status_code, response_json FROM hz_idempotency "
                                 "WHERE caller=:c AND route=:r AND key=:k"), params).mappings().first()
        if row is None:
            raise WorkItemError("unavailable", "Could not reserve the Idempotency-Key. Try again.",
                                status=503, retryable=True)
        if row["digest"] != digest:
            raise WorkItemError("idempotency_key_reused",
                                "This Idempotency-Key was already used with a different request. "
                                "Use a new key for a new request.", status=409)
        if row["status_code"] == 0:
            raise WorkItemError("request_in_progress", "The first request with this key is still being "
                                "handled. Try again shortly with the same key.", status=409, retryable=True)
        return row["status_code"], json.loads(row["response_json"])
    try:
        status, response = produce()
    except BaseException:
        with engine(hub_dir).begin() as c:   # nothing was recorded: free the key for a retry
            c.execute(text("DELETE FROM hz_idempotency WHERE caller=:c AND route=:r AND key=:k "
                           "AND status_code=0"), params)
        raise
    with engine(hub_dir).begin() as c:
        c.execute(text("UPDATE hz_idempotency SET status_code=:s, response_json=:j "
                       "WHERE caller=:c AND route=:r AND key=:k"),
                  dict(params, s=status, j=dumps(response)))
    return status, response
