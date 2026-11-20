"""Work orders: one ``hz_runner_calls`` row per call to an AgentCore runner.

Why a runner may act: only the hub service's role may invoke the runtime (an
AWS resource policy), and every request must match a row the hub service wrote
first. The runner claims the row once, in one statement, only while it is
fresh and its digest matches the request it received. After that the row is
the call's authority: the runner checks ``state`` before every tool step, the
hub service sets ``cancel_requested`` to stop it, and the runner ends it.

While it waits, the hub service renews ``heartbeat_at``. A call whose hub
service went away (stopped, restarted, stalled) gets no more renewals, and the
runner stops it within ``LIVE_SECONDS``: no new tool step, the turn cancelled.

States: ``created`` -> ``running`` -> ``ended``, with ``cancel_requested``
between the last two when the hub service stops a call.
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from sqlalchemy import text

#: A work order not claimed within this many seconds is refused.
CLAIM_SECONDS = 300
#: How often the hub service renews a call it waits for, and how long the
#: runner goes on without a renewal.
HEARTBEAT_SECONDS = 10
LIVE_SECONDS = 45
TERMINAL = "ended"


def _engine(hub_dir: Path):
    from .. import db, migrations

    engine = db.operational_engine(Path(hub_dir))
    migrations.upgrade(engine, "operational")
    return engine


def new_id() -> str:
    return "rc_" + secrets.token_urlsafe(24)


def create(hub_dir: Path, *, call_id: str, hub: str, kind: str, session_id: str,
           request_digest: str, identity: dict | None) -> None:
    """The hub service's side: write the work order before invoking the runner."""
    with _engine(hub_dir).begin() as c:
        c.execute(text(
            "INSERT INTO hz_runner_calls (id, hub, kind, session_id, state, request_digest, identity, created_at, "
            "heartbeat_at) VALUES (:id, :hub, :kind, :sid, 'created', :dg, :ident, :now, :now)"),
            {"id": call_id, "hub": hub, "kind": kind, "sid": session_id, "dg": request_digest,
             "ident": json.dumps(identity, default=str) if identity is not None else None, "now": time.time()})


def claim(hub_dir: Path, *, call_id: str, request_digest: str, session_id: str,
          runner_version: str) -> dict | None:
    """The runner's side: take the work order once. Returns the row, or None when
    there is none, it was already taken, it is stale, it belongs to another
    session or the request differs from what the hub service recorded."""
    now = time.time()
    with _engine(hub_dir).begin() as c:
        claimed = c.execute(text(
            "UPDATE hz_runner_calls SET state='running', claimed_at=:now, heartbeat_at=:now, runner_version=:ver "
            "WHERE id=:id AND state='created' AND request_digest=:dg AND session_id=:sid AND created_at>=:fresh"),
            {"now": now, "ver": runner_version, "id": call_id, "dg": request_digest, "sid": session_id,
             "fresh": now - CLAIM_SECONDS}).rowcount
        if claimed != 1:
            return None
        row = c.execute(text("SELECT * FROM hz_runner_calls WHERE id=:id"), {"id": call_id}).mappings().first()
    out = dict(row)
    out["identity"] = json.loads(out["identity"]) if out.get("identity") else None
    return out


def refuse(hub_dir: Path, *, call_id: str, reason: str) -> None:
    """Record why a call could not be claimed (the row may not exist)."""
    with _engine(hub_dir).begin() as c:
        c.execute(text("UPDATE hz_runner_calls SET state='ended', outcome='refused', error=:e, ended_at=:now "
                       "WHERE id=:id AND state='created'"), {"e": reason[:500], "now": time.time(), "id": call_id})


def state(hub_dir: Path, call_id: str) -> str | None:
    with _engine(hub_dir).connect() as c:
        return c.execute(text("SELECT state FROM hz_runner_calls WHERE id=:id"), {"id": call_id}).scalar()


def running(hub_dir: Path, call_id: str) -> bool:
    """May this call go on (another tool step, more model output): claimed, not
    stopped, and its hub service renewed it within ``LIVE_SECONDS``."""
    with _engine(hub_dir).connect() as c:
        row = c.execute(text("SELECT state, heartbeat_at FROM hz_runner_calls WHERE id=:id"),
                        {"id": call_id}).first()
    return bool(row) and row[0] == "running" and (row[1] or 0) >= time.time() - LIVE_SECONDS


def beat(hub_dir: Path, call_id: str) -> bool:
    """The hub service's side: still waiting for this call. False once it ended."""
    with _engine(hub_dir).begin() as c:
        return c.execute(text("UPDATE hz_runner_calls SET heartbeat_at=:now WHERE id=:id "
                              "AND state IN ('created','running')"), {"now": time.time(), "id": call_id}).rowcount == 1


def request_cancel(hub_dir: Path, call_id: str) -> bool:
    """The hub service's side: ask a call to stop. False when it already ended."""
    with _engine(hub_dir).begin() as c:
        return c.execute(text(
            "UPDATE hz_runner_calls SET state='cancel_requested' WHERE id=:id AND state IN ('created','running')"),
            {"id": call_id}).rowcount == 1


def end(hub_dir: Path, call_id: str, *, outcome: str, error: str | None = None) -> None:
    with _engine(hub_dir).begin() as c:
        c.execute(text("UPDATE hz_runner_calls SET state='ended', outcome=:o, error=:e, ended_at=:now "
                       "WHERE id=:id AND state<>'ended'"),
                  {"o": outcome, "e": (error or None) and error[:2000], "now": time.time(), "id": call_id})


def active_in_session(hub_dir: Path, session_id: str, *, besides: str | None = None) -> int:
    """How many calls are still running in one AgentCore session."""
    with _engine(hub_dir).connect() as c:
        return int(c.execute(text(
            "SELECT COUNT(*) FROM hz_runner_calls WHERE session_id=:sid "
            "AND state IN ('created','running','cancel_requested') "
            "AND id<>:b"), {"sid": session_id, "b": besides or ""}).scalar() or 0)
