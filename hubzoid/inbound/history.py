"""Per-chat conversation history, stored in the hub-owned database.

Slack and Open WebUI give the agent memory by sending the whole conversation
array on every turn (Slack reads its thread back, OWUI its DB). The bridge is
stateless and just flattens whatever array it receives. WhatsApp/Telegram don't
replay the thread, so the harness keeps the recent turns per ``chat_id`` here and
sends them — the same mechanism, we just hold the thread.

Backed by the hub database (SQLite by default, Postgres via ``DATABASE_URL``),
in our own ``hz_``-prefixed table — never Open WebUI's schema. Bounded by design:
only the last ``max_messages`` per chat are kept (a chat running for months has
the same footprint as a fresh one), with an optional TTL to drop stale turns.
Isolated per ``chat_id`` — a PII guarantee.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
)
from sqlalchemy.engine import Engine

log = logging.getLogger("hubzoid.inbound")

DEFAULT_MAX_MESSAGES = 40  # ~20 turns; override per hub with INBOUND_HISTORY_MAX

_metadata = MetaData()
_history = Table(
    "hz_inbound_history",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("chat_id", String(255), index=True),
    Column("role", String(32)),
    Column("content", Text),
    Column("created_at", Float),
)


class History:
    def __init__(self, engine: Engine, max_messages: int = DEFAULT_MAX_MESSAGES,
                 ttl_seconds: "float | None" = None) -> None:
        self.engine = engine
        self.max = max(2, int(max_messages))
        self.ttl = ttl_seconds
        _metadata.create_all(engine, tables=[_history])  # CREATE TABLE IF NOT EXISTS

    def load(self, chat_id: str) -> "list[dict]":
        """Return the recent messages for `chat_id` (oldest first, capped).

        Best-effort: a transient DB read error degrades to no prior context (an
        empty list) rather than dropping the user's reply."""
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(
                    select(_history.c.role, _history.c.content)
                    .where(_history.c.chat_id == chat_id)
                    .order_by(_history.c.id.desc())
                    .limit(self.max)
                ).all()
        except Exception:  # noqa: BLE001 — memory is best-effort; never break a reply
            log.warning("history: load failed; continuing with no prior context", exc_info=True)
            return []
        return [{"role": r.role, "content": r.content} for r in reversed(rows)]

    def append(self, chat_id: str, role: str, content: str, _now: "float | None" = None) -> None:
        """Append one message, then prune to the cap (and past the TTL). Blank is a no-op."""
        if not content or not content.strip():
            return
        now = _now if _now is not None else time.time()
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(_history).values(
                    chat_id=chat_id, role=role, content=content, created_at=now))
                keep = conn.execute(
                    select(_history.c.id)
                    .where(_history.c.chat_id == chat_id)
                    .order_by(_history.c.id.desc())
                    .limit(self.max)
                ).scalars().all()
                if keep:
                    conn.execute(delete(_history).where(
                        _history.c.chat_id == chat_id, _history.c.id.not_in(keep)))
                if self.ttl:
                    conn.execute(delete(_history).where(
                        _history.c.chat_id == chat_id,
                        _history.c.created_at < now - self.ttl))
        except Exception:  # noqa: BLE001 — history is best-effort; never break a reply
            log.warning("history: append failed for a chat", exc_info=True)
