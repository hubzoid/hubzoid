"""Idempotent writes: one ``Idempotency-Key`` gives one result.

A POST or PUT that creates something or starts work accepts an
``Idempotency-Key`` header (``--idempotency-key`` on the CLI). The key is
scoped to the deployment, hub, caller and route and kept for at least 24
hours. A replay with the same body returns the first result. The same key with
a different body is 409 ``idempotency_key_reused``.

The domain change and its stored response commit in ONE transaction (``run``
hands the open connection to the change), so a crash can never leave a change
without its response or a response without its change. A request that fails
stores nothing, so it can be retried with the same key.

The table is ``hz_chat_operations`` (migration ``op_0020``), shared by every
chat and control-plane route.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from .canonical import request_hash

metadata = sa.MetaData()

RETAIN_SECONDS = 24 * 3600
#: Stands for "no hub" in the unique scope (NULLs would not collide).
DEPLOYMENT_SCOPE = "*"

operations = sa.Table(
    "hz_chat_operations", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("deployment_id", sa.String(64), nullable=False),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("actor_id", sa.String(320), nullable=False),
    sa.Column("route", sa.String(255), nullable=False),
    sa.Column("idempotency_key", sa.String(255), nullable=False),
    sa.Column("request_hash", sa.String(64), nullable=False),
    sa.Column("kind", sa.String(64), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("resource_refs_json", sa.Text),
    sa.Column("result_json", sa.Text),
    sa.Column("error_json", sa.Text),
    sa.Column("operation_id", sa.String(64)),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.UniqueConstraint("deployment_id", "hub_id", "actor_id", "route", "idempotency_key",
                        name="hz_chat_operations_scope"),
)

MAX_KEY = 255


@dataclass
class Outcome:
    status: int
    body: Any
    replayed: bool = False


def reused() -> HTTPException:
    return HTTPException(409, detail={
        "code": "idempotency_key_reused",
        "message": "That Idempotency-Key was already used for a different request. Use a new key.",
    })


def check_key(key: str | None) -> str | None:
    if key is None:
        return None
    key = key.strip()
    if not key or len(key) > MAX_KEY:
        raise HTTPException(400, detail={"code": "invalid_request",
                                         "message": "Idempotency-Key must be 1 to 255 characters."})
    return key


def run(engine, *, deployment_id: str, actor_id: str, route: str, key: str | None, body: Any,
        change: Callable[[Any], tuple[int, Any]], hub_id: str | None = None,
        kind: str = "", now: float | None = None) -> Outcome:
    """Run ``change(conn) -> (status, body)`` once per key. Without a key the
    change simply runs in its own transaction."""
    key = check_key(key)
    now = time.time() if now is None else now
    hub = hub_id or DEPLOYMENT_SCOPE
    if key is None:
        with engine.begin() as conn:
            status, result = change(conn)
        return Outcome(status, result)
    rhash = request_hash(body)
    c = operations.c
    scope = sa.and_(c.deployment_id == deployment_id, c.hub_id == hub, c.actor_id == actor_id,
                    c.route == route, c.idempotency_key == key)
    for _attempt in range(2):
        try:
            with engine.begin() as conn:
                row = conn.execute(sa.select(operations).where(scope)).first()
                if row is not None and row.expires_at < now:
                    conn.execute(operations.delete().where(c.id == row.id))
                    row = None
                if row is not None:
                    if row.request_hash != rhash:
                        raise reused()
                    stored = json.loads(row.result_json or "{}")
                    return Outcome(int(stored.get("status", 200)), stored.get("body"), True)
                status, result = change(conn)
                conn.execute(operations.insert().values(
                    id=str(uuid.uuid4()), deployment_id=deployment_id, hub_id=hub,
                    actor_id=actor_id, route=route, idempotency_key=key, request_hash=rhash,
                    kind=kind or route, state="completed", resource_refs_json=None,
                    result_json=json.dumps({"status": status, "body": result}, default=str),
                    error_json=None, operation_id=None, created_at=now,
                    expires_at=now + RETAIN_SECONDS))
                return Outcome(status, result)
        except IntegrityError:
            # A concurrent request with the same key committed first: read it.
            continue
    raise HTTPException(409, detail={"code": "request_in_progress",
                                     "message": "The same request is still being handled. Try again."})


def purge(engine, now: float | None = None) -> int:
    now = time.time() if now is None else now
    with engine.begin() as conn:
        return conn.execute(operations.delete().where(operations.c.expires_at < now)).rowcount
