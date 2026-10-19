"""Admission: verify, normalize and commit a provider delivery before the
provider gets its answer.

The order is fixed:

1. The connection must exist. Its adapter verifies the signature on the exact
   received bytes, before anything is parsed.
2. A protocol handshake (`Challenge`) is answered without recording work, even
   when the connection is paused.
3. The adapter turns the delivery into events. All of them, the thread rows
   and their outcomes commit in one transaction, or none do.
4. Only then does the caller acknowledge the provider. A storage failure is a
   retryable error to the provider, never a success.

A repeat delivery with the same bytes returns the first outcome. A reused
delivery id with different bytes is quarantined and never runs. The same event
under another delivery id (Slack's `message` and `app_mention` for one
message, a GitHub redelivery) is recorded as a duplicate of the first.

This follows Agno's "verify, then deduplicate by event id before background
work" (``libs/agno/agno/os/interfaces/slack/router.py:145-189``), with the
in-memory deduplicator it documents as process-local
(``helpers.py:186-219``) replaced by a database receipt.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text

from .. import secretbox
from . import config as config_lib
from . import store
from .config import Connection
from .errors import ChannelError
from .models import Challenge, InboundEvent, Verified

log = logging.getLogger("hubzoid.channels.admission")

#: accepted deliveries waiting for work, per connection, before new work is refused
BACKLOG_LIMIT = 10_000


@dataclass
class Admitted:
    """The outcome of one delivery: what the provider is told, and each event."""

    delivery_id: str
    status: str
    events: list[dict] = field(default_factory=list)
    duplicate: bool = False

    def to_json(self) -> dict:
        return {"delivery_id": self.delivery_id, "status": self.status, "duplicate": self.duplicate,
                "events": [{"id": e["id"], "status": e["status"], "reason": e.get("reason"),
                            "thread_id": e.get("thread_id")} for e in self.events]}


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def receive(hub_dir, cid: str, raw: bytes, headers: dict, query: dict) -> Admitted | Challenge:
    """The webhook path: verify, parse, commit. Raises ChannelError for a
    refusal (the route turns it into the provider-facing status)."""
    hub_dir = Path(hub_dir)
    conn = config_lib.active(hub_dir, cid)
    adapter = conn.adapter
    if len(raw) > adapter.body_limit:
        raise ChannelError("delivery_too_large", "The request body is larger than this channel accepts.",
                           status=413)
    result = adapter.verify(conn, raw, {k.lower(): v for k, v in headers.items()}, dict(query))
    if isinstance(result, Challenge):
        return result
    events = adapter.parse(conn, result)
    return admit(conn, result, events, raw=raw)


def admit(conn: Connection, verified: Verified, events: list[InboundEvent], *, raw: bytes | None = None
          ) -> Admitted:
    """Commit a verified delivery's events. Safe to call again with the same
    delivery: it returns the first outcome."""
    hub_dir, hub = conn.hub_dir, conn.hub
    body_digest = digest(raw if raw is not None else verified.body)
    rows: list[dict] = []
    dups: list[bool] = []
    duplicate = False
    try:
        with store.locked(hub_dir, f"channel-admit:{hub}:{conn.id}") as c:
            if not events:
                # A verified delivery with nothing to do still leaves a receipt,
                # so a redelivery is recognised and the Console can show it.
                events = []
                existing = store.find_delivery(c, hub=hub, cid=conn.id, delivery_id=verified.delivery_id,
                                               index=0)
                if existing is None:
                    rows.append(store.insert_delivery(
                        c, hub=hub, connection_id=conn.id, delivery_id=verified.delivery_id, event_index=0,
                        body_digest=body_digest, kind="none", status="ignored", reason="no_supported_event",
                        verification=verified.method))
                else:
                    duplicate = True
                    rows.append(existing)
                dups.append(existing is not None)
            backlog = _backlog(c, hub, conn.id)
            for index, event in enumerate(events):
                row, dup = _admit_one(c, conn, verified, event, index, body_digest, backlog)
                duplicate = duplicate or dup
                dups.append(dup)
                if row["status"] == "accepted":
                    backlog += 1
                rows.append(row)
    except ChannelError:
        raise
    except Exception as exc:  # noqa: BLE001 - never acknowledge what was not saved
        log.exception("channel %s: could not record delivery %s", conn.id, verified.delivery_id)
        raise ChannelError("unavailable", "The delivery could not be saved. Send it again.", status=503,
                           retryable=True) from exc
    if any(r["status"] == "accepted" for r in rows):
        from . import router

        router.kick(hub_dir)
    status = "duplicate" if dups and all(dups) else "accepted"
    if rows and all(r["status"] == "quarantined" for r in rows):
        status = "quarantined"
    return Admitted(verified.delivery_id, status, rows, duplicate)


def _backlog(c, hub: str, cid: str) -> int:
    return c.execute(text("SELECT COUNT(*) FROM hz_channel_deliveries WHERE hub=:h AND connection_id=:c "
                          "AND status IN ('accepted','waiting')"), {"h": hub, "c": cid}).scalar() or 0


def _admit_one(c, conn: Connection, verified: Verified, event: InboundEvent, index: int, body_digest: str,
               backlog: int) -> tuple[dict, bool]:
    hub, cid = conn.hub, conn.id
    existing = store.find_delivery(c, hub=hub, cid=cid, delivery_id=verified.delivery_id, index=index)
    if existing is not None:
        if existing["body_digest"] == body_digest:
            return existing, True
        # The same delivery id with different bytes: keep the evidence, run nothing.
        n = c.execute(text("SELECT COUNT(*) FROM hz_channel_deliveries WHERE hub=:h AND connection_id=:c AND "
                           "delivery_id LIKE :d"), {"h": hub, "c": cid,
                                                    "d": f"{verified.delivery_id}#conflict-%"}).scalar() or 0
        return store.insert_delivery(
            c, hub=hub, connection_id=cid, delivery_id=f"{verified.delivery_id}#conflict-{n + 1}",
            event_index=index, body_digest=body_digest, kind=event.kind, status="quarantined",
            reason="delivery_conflict", verification=verified.method,
            route_json={"original": existing["id"]}), False
    base = dict(hub=hub, connection_id=cid, delivery_id=verified.delivery_id, event_index=index,
                body_digest=body_digest, kind=event.kind, verification=verified.method,
                actor_json=event.actor.to_json() if event.actor else {})
    same = store.find_semantic(c, hub=hub, cid=cid, semantic_key=event.semantic_key)
    if same is not None and event.trigger and same["status"] == "ignored" and same["reason"] == "not_addressed":
        # The same message seen first without its mention (Slack sends both a
        # message and an app_mention): the mention upgrades it, exactly once.
        thread = store.get_thread(conn.hub_dir, same["thread_id"], conn=c)
        status, reason = _disposition(conn, event, thread, backlog, c)
        upgraded = store.update_delivery(conn.hub_dir, same["id"], expect_status="ignored", conn=c,
                                         status=status, reason=reason, kind=event.kind,
                                         event_enc=secretbox.encrypt_json(conn.hub_dir, event.to_json()))
        if upgraded is not None:
            store.insert_delivery(c, **base, status="duplicate", reason="same_event", thread_id=same["thread_id"],
                                  route_json={"original": same["id"], "upgraded": True})
            return upgraded, False
    if same is not None:
        # Another delivery already carried this event. Link to it, do nothing.
        return store.insert_delivery(c, **base, status="duplicate", reason="same_event",
                                     thread_id=same["thread_id"], route_json={"original": same["id"]}), True
    thread, _ = store.thread_for(c, hub=hub, cid=cid, origin=event.origin.to_json())
    enc = secretbox.encrypt_json(conn.hub_dir, event.to_json())
    status, reason = _disposition(conn, event, thread, backlog, c)
    row = store.insert_delivery(c, **base, semantic_key=event.semantic_key, thread_id=thread["id"],
                                event_enc=enc, status=status, reason=reason)
    if status == "accepted":
        c.execute(text("UPDATE hz_channel_threads SET last_event_at=:t, updated=:t WHERE id=:i"),
                  {"t": store.now(), "i": thread["id"]})
    return row, False


def _disposition(conn: Connection, event: InboundEvent, thread: dict, backlog: int, c) -> tuple[str, str | None]:
    if event.ignore_reason:
        return "ignored", event.ignore_reason
    if event.actor is not None and event.actor.is_bot:
        return "ignored", "bot_author"
    addressed = event.trigger or bool(thread.get("subscribed"))
    if not addressed:
        return "ignored", "not_addressed"
    if not conn.enabled:
        return "disabled", "connection_disabled"
    if backlog >= BACKLOG_LIMIT:
        return "denied", "backlog_full"
    waiting = c.execute(text("SELECT COUNT(*) FROM hz_channel_deliveries WHERE thread_id=:t AND "
                             "status IN ('accepted','waiting')"), {"t": thread["id"]}).scalar() or 0
    if waiting >= conn.config.queue_limit:
        return "denied", "queue_full"
    return "accepted", None


def event_of(hub_dir, delivery: dict) -> InboundEvent:
    """The normalized event a delivery row carries."""
    return InboundEvent.from_json(secretbox.decrypt_json(Path(hub_dir), delivery["event_enc"]))
