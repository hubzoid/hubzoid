"""The conversation graph's runtime records: writer branches, personal views,
runs, branch leases, the durable queue, the event log and copy lineage.

Messages already form a tree (``store.messages``, ``parent_id``). On top of it:

* A **branch** is one writer's line of work: its ``writer_actor_id``, the
  message it started from (``base_message_id``) and its current ``head_id``.
  ``send``, ``respond`` and ``synthesize`` extend a branch, and only from its
  head and only by its writer. ``revise``, ``regenerate`` and ``continue`` add
  a sibling node and so open a new branch for the person who asked. Each branch
  has an integer ``revision`` that moves on every write.
* A **view** is one person's selected branch and head. Changing it never
  touches the graph or anyone else's view.
* A **run** is one assistant reply being written. One run at a time may hold a
  branch's **lease**; different branches run concurrently, within the
  configured limits. The lease is renewed while the run lives and carries a
  fence, so a run whose process died cannot write after recovery.
* The **queue** holds acknowledged follow-ups per branch, in server order
  (steer lane first). Stop parks it. Removing an item parks the items that
  depend on it.
* The **event log** gives every conversation a strictly increasing sequence,
  so a client that reconnects asks for what it missed instead of guessing.

Every write here runs inside the caller's transaction, together with its event.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

import sqlalchemy as sa

from .store import conversations, messages, metadata

branches = sa.Table(
    "hz_chat_branches", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("conversation_id", sa.String(64), nullable=False, index=True),
    sa.Column("writer_actor_id", sa.String(64), nullable=False),
    sa.Column("base_message_id", sa.String(64)),
    sa.Column("head_id", sa.String(64)),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("queue_paused", sa.Integer, nullable=False),
    sa.Column("queue_revision", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

views = sa.Table(
    "hz_chat_views", metadata,
    sa.Column("conversation_id", sa.String(64), primary_key=True),
    sa.Column("account_id", sa.String(64), primary_key=True),
    sa.Column("branch_id", sa.String(64)),
    sa.Column("view_head_id", sa.String(64)),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

runs = sa.Table(
    "hz_chat_runs", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("conversation_id", sa.String(64), nullable=False, index=True),
    sa.Column("branch_id", sa.String(64), nullable=False),
    sa.Column("message_id", sa.String(64), nullable=False),
    sa.Column("actor_id", sa.String(64), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("input_snapshot", sa.Text, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("cancel_requested", sa.Integer, nullable=False),
    sa.Column("operation_id", sa.String(64)),
    sa.Column("error", sa.Text),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

leases = sa.Table(
    "hz_chat_branch_leases", metadata,
    sa.Column("branch_id", sa.String(64), primary_key=True),
    sa.Column("conversation_id", sa.String(64), nullable=False),
    sa.Column("actor_id", sa.String(64), nullable=False),
    sa.Column("run_id", sa.String(64), nullable=False),
    sa.Column("owner", sa.String(128), nullable=False),
    sa.Column("fence", sa.Integer, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
)

queue = sa.Table(
    "hz_chat_queue", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("conversation_id", sa.String(64), nullable=False, index=True),
    sa.Column("branch_id", sa.String(64), nullable=False),
    sa.Column("author_id", sa.String(64), nullable=False),
    sa.Column("lane", sa.String(8), nullable=False),
    sa.Column("rank", sa.Float, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("content_json", sa.Text, nullable=False),
    sa.Column("predecessor_id", sa.String(64)),
    sa.Column("reason", sa.Text),
    sa.Column("dispatched_run_id", sa.String(64)),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

events = sa.Table(
    "hz_chat_events", metadata,
    sa.Column("conversation_id", sa.String(64), primary_key=True),
    sa.Column("sequence", sa.Integer, primary_key=True),
    sa.Column("type", sa.String(32), nullable=False),
    sa.Column("object_id", sa.String(64)),
    sa.Column("graph_revision", sa.Integer, nullable=False),
    sa.Column("payload_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)

lineage = sa.Table(
    "hz_chat_copy_lineage", metadata,
    sa.Column("new_message_id", sa.String(64), primary_key=True),
    sa.Column("source_conversation_id", sa.String(64), nullable=False),
    sa.Column("source_message_id", sa.String(64), nullable=False),
    sa.Column("copy_operation_id", sa.String(64), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)

members = sa.Table(
    "hz_chat_members", metadata,
    sa.Column("conversation_id", sa.String(64), primary_key=True),
    sa.Column("principal_type", sa.String(8), primary_key=True),
    sa.Column("principal_id", sa.String(320), primary_key=True),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("added_by", sa.String(64), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)

member_sets = sa.Table(
    "hz_chat_member_sets", metadata,
    sa.Column("conversation_id", sa.String(64), primary_key=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

ACTIVE_RUN = ("admitted", "streaming", "waiting", "stopping")
TERMINAL_RUN = ("complete", "stopped", "interrupted", "failed")
QUEUE_LIVE = ("queued", "paused", "needs_review", "blocked")
LEASE_SECONDS = 30.0
EVENT_RETAIN = 5000  # events kept per conversation for replay


class GraphError(Exception):
    """A refused graph operation (``status``, snake_case ``code``)."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = {k: v for k, v in extra.items() if v is not None}


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> float:
    return time.time()


# -- events -------------------------------------------------------------------------
def emit(conn, conversation_id: str, type_: str, object_id: str | None = None,
         payload: dict | None = None) -> int:
    """Append one event; returns its sequence. Bumps the conversation's
    graph revision for graph-changing types."""
    graph_changing = type_ in ("message.created", "message.revised", "graph.deleted", "branch.created",
                               "conversation.deleted")
    rev = conn.execute(sa.select(conversations.c.graph_revision)
                       .where(conversations.c.id == conversation_id)).scalar() or 0
    if graph_changing:
        rev += 1
        conn.execute(conversations.update().where(conversations.c.id == conversation_id)
                     .values(graph_revision=rev))
    last = conn.execute(sa.select(sa.func.max(events.c.sequence))
                        .where(events.c.conversation_id == conversation_id)).scalar() or 0
    seq = last + 1
    conn.execute(events.insert().values(conversation_id=conversation_id, sequence=seq, type=type_,
                                        object_id=object_id, graph_revision=rev,
                                        payload_json=json.dumps(payload or {}, default=str)[:20000],
                                        created_at=now()))
    if seq % 500 == 0:
        conn.execute(events.delete().where(events.c.conversation_id == conversation_id,
                                           events.c.sequence <= seq - EVENT_RETAIN))
    return seq


def events_after(engine, conversation_id: str, after: int, limit: int = 500) -> list[dict]:
    with engine.connect() as conn:
        oldest = conn.execute(sa.select(sa.func.min(events.c.sequence))
                              .where(events.c.conversation_id == conversation_id)).scalar()
        if oldest is not None and after and after < oldest - 1:
            raise GraphError(410, "replay_expired",
                             "Those events are no longer kept. Reload the conversation.")
        rows = conn.execute(sa.select(events).where(events.c.conversation_id == conversation_id,
                                                    events.c.sequence > after)
                            .order_by(events.c.sequence).limit(limit)).all()
    return [{"sequence": r.sequence, "type": r.type, "object_id": r.object_id,
             "graph_revision": r.graph_revision, "payload": json.loads(r.payload_json),
             "created_at": r.created_at} for r in rows]


def cursor(conn, conversation_id: str) -> int:
    return conn.execute(sa.select(sa.func.max(events.c.sequence))
                        .where(events.c.conversation_id == conversation_id)).scalar() or 0


# -- branches -------------------------------------------------------------------------
def branch_row(conn, branch_id: str):
    return conn.execute(sa.select(branches).where(branches.c.id == branch_id)).first()


def branch_json(row) -> dict:
    return {"id": row.id, "writer_actor_id": row.writer_actor_id, "base_message_id": row.base_message_id,
            "head_id": row.head_id, "revision": row.revision, "state": row.state,
            "queue_paused": bool(row.queue_paused), "queue_revision": row.queue_revision,
            "created_at": row.created_at, "updated_at": row.updated_at}


def create_branch(conn, conversation_id: str, actor_id: str, base_message_id: str | None,
                  head_id: str | None = None) -> dict:
    count = conn.execute(sa.select(sa.func.count()).select_from(branches)
                         .where(branches.c.conversation_id == conversation_id)).scalar() or 0
    if count >= 1000:
        raise GraphError(429, "limit_reached", "This conversation has 1,000 branches. Fork it to keep going.")
    bid = new_id()
    t = now()
    conn.execute(branches.insert().values(id=bid, conversation_id=conversation_id, writer_actor_id=actor_id,
                                          base_message_id=base_message_id,
                                          head_id=head_id if head_id is not None else base_message_id,
                                          revision=1, state="open", queue_paused=0, queue_revision=1,
                                          created_at=t, updated_at=t))
    emit(conn, conversation_id, "branch.created", bid, {"base_message_id": base_message_id,
                                                       "writer_actor_id": actor_id})
    return branch_json(branch_row(conn, bid))


def ensure_initial_branch(conn, conv: dict, actor_id: str) -> None:
    """A conversation from before branches existed gets one branch per leaf, the
    owner as writer, so its whole tree stays reachable."""
    has = conn.execute(sa.select(branches.c.id).where(branches.c.conversation_id == conv["id"]).limit(1)).first()
    if has is not None:
        return
    rows = conn.execute(sa.select(messages.c.id, messages.c.parent_id)
                        .where(messages.c.conversation_id == conv["id"])
                        .order_by(messages.c.created_at, messages.c.id)).all()
    parents = {p for _, p in rows if p}
    leaves = [mid for mid, _ in rows if mid not in parents] or [None]
    t = now()
    for leaf in leaves:
        conn.execute(branches.insert().values(id=new_id(), conversation_id=conv["id"],
                                              writer_actor_id=conv["owner_id"], base_message_id=None,
                                              head_id=leaf, revision=1, state="open", queue_paused=0,
                                              queue_revision=1, created_at=t, updated_at=t))


def move_head(conn, branch_id: str, expected_revision: int | None, head_id: str) -> int:
    row = branch_row(conn, branch_id)
    if expected_revision is not None and row.revision != expected_revision:
        raise GraphError(409, "branch_conflict", "This branch changed since you loaded it. Reload and try again.",
                         current_revision=row.revision)
    done = conn.execute(branches.update().where(branches.c.id == branch_id, branches.c.revision == row.revision)
                        .values(head_id=head_id, revision=row.revision + 1, updated_at=now())).rowcount
    if done != 1:
        raise GraphError(409, "branch_conflict", "This branch changed at the same moment. Reload and try again.",
                         current_revision=row.revision + 1)
    return row.revision + 1


# -- views ---------------------------------------------------------------------------
def get_view(conn, conversation_id: str, account_id: str) -> dict | None:
    row = conn.execute(sa.select(views).where(views.c.conversation_id == conversation_id,
                                              views.c.account_id == account_id)).first()
    if row is None:
        return None
    return {"branch_id": row.branch_id, "view_head_id": row.view_head_id, "revision": row.revision}


def set_view(conn, conversation_id: str, account_id: str, branch_id: str | None, head_id: str | None,
             expected_revision: int | None = None) -> dict:
    row = conn.execute(sa.select(views).where(views.c.conversation_id == conversation_id,
                                              views.c.account_id == account_id)).first()
    if row is None:
        conn.execute(views.insert().values(conversation_id=conversation_id, account_id=account_id,
                                           branch_id=branch_id, view_head_id=head_id, revision=1,
                                           updated_at=now()))
        return {"branch_id": branch_id, "view_head_id": head_id, "revision": 1}
    if expected_revision is not None and row.revision != expected_revision:
        raise GraphError(409, "graph_conflict", "Your view changed in another tab. Reload it.",
                         current_revision=row.revision)
    conn.execute(views.update().where(views.c.conversation_id == conversation_id,
                                      views.c.account_id == account_id)
                 .values(branch_id=branch_id, view_head_id=head_id, revision=row.revision + 1, updated_at=now()))
    return {"branch_id": branch_id, "view_head_id": head_id, "revision": row.revision + 1}


# -- leases --------------------------------------------------------------------------
def acquire_lease(conn, *, conversation_id: str, branch_id: str, actor_id: str, run_id: str, owner: str,
                  max_per_conversation: int, max_per_actor: int, t: float | None = None) -> int:
    """Take the branch's writer lease for ``run_id``. Raises ``branch_busy``
    or ``concurrency_limit``. Returns the fence."""
    t = now() if t is None else t
    existing = conn.execute(sa.select(leases).where(leases.c.branch_id == branch_id)).first()
    if existing is not None and existing.expires_at > t:
        raise GraphError(409, "branch_busy", "A reply is still being written on this branch. Stop it, wait, "
                                             "or queue your message.", retryable=True)
    active = conn.execute(sa.select(leases.c.actor_id).where(leases.c.conversation_id == conversation_id,
                                                             leases.c.expires_at > t)).all()
    if len(active) >= max_per_conversation:
        raise GraphError(429, "concurrency_limit", f"This conversation already runs {len(active)} branches at "
                                                   "once. Wait for one to finish.", retry_after=5)
    if sum(1 for (a,) in active if a == actor_id) >= max_per_actor:
        raise GraphError(429, "concurrency_limit", "You already have the most replies running here at once. "
                                                   "Wait for one to finish.", retry_after=5)
    fence = (existing.fence + 1) if existing is not None else 1
    if existing is not None:
        # An expired lease: its run is reconciled to interrupted by the owner sweep.
        conn.execute(leases.delete().where(leases.c.branch_id == branch_id, leases.c.fence == existing.fence))
    conn.execute(leases.insert().values(branch_id=branch_id, conversation_id=conversation_id, actor_id=actor_id,
                                        run_id=run_id, owner=owner, fence=fence, expires_at=t + LEASE_SECONDS))
    return fence


def renew_lease(engine, branch_id: str, run_id: str, fence: int) -> bool:
    with engine.begin() as conn:
        return conn.execute(leases.update().where(leases.c.branch_id == branch_id, leases.c.run_id == run_id,
                                                  leases.c.fence == fence)
                            .values(expires_at=now() + LEASE_SECONDS)).rowcount == 1


def release_lease(conn, branch_id: str, run_id: str) -> None:
    conn.execute(leases.delete().where(leases.c.branch_id == branch_id, leases.c.run_id == run_id))
