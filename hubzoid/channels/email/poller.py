"""Reading new mail: one poller per email connection, one worker at a time.

Each pass takes a short lease on the connection (so two bridge processes do
not read the same mailbox at once), asks the transport for mail after the
saved cursor, and for each message:

1. keeps the raw source in the hub's file service (so attachments can be
   fetched again later and a person can inspect exactly what arrived),
2. parses it under limits and runs the sender check (allowlist and aligned
   DKIM),
3. commits it through the channel's admission like any webhook delivery,
   keyed by the provider's message id, deduplicated by Message-ID,

and only then moves the cursor. A crash between steps re-reads the message
and admission recognises it. Nothing is marked read or moved in the mailbox.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time

from .. import admission, store
from ..errors import ChannelError
from ..models import ExternalActor, InboundEvent, Origin, Verified
from . import mime, transports

log = logging.getLogger("hubzoid.channels.email")


class Poller:
    def __init__(self, conn):
        self.conn = conn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.owner = secrets.token_hex(8)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"email-{self.conn.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        last = None
        while not self._stop.is_set():
            try:
                poll_once(self.conn, owner=self.owner)
                last = None
            except ChannelError as exc:     # expected states (not connected, storage full): once per change
                if exc.code != last:
                    log.warning("channel %s: mail not read: %s (%s)", self.conn.id, exc.message, exc.code)
                last = exc.code
            except Exception:  # noqa: BLE001 - logged; the next pass tries again
                log.exception("channel %s: mail poll failed", self.conn.id)
                last = None
            self._stop.wait(self.conn.settings.poll_seconds)


def _lease(conn, owner: str, seconds: float) -> bool:
    def take(state: dict):
        lease = state.get("email_poll_lease") or {}
        if lease.get("owner") not in (None, owner) and float(lease.get("until") or 0) > time.time():
            return None, False
        return {"email_poll_lease": {"owner": owner, "until": time.time() + seconds}}, True

    return store.swap_connection_state(conn.hub_dir, conn.hub, conn.id, take)


def poll_once(conn, *, owner: str = "cli") -> dict:
    """One pass. Returns what it saw, for the CLI and tests."""
    from .. import config as config_lib

    conn = config_lib.active(conn.hub_dir, conn.id)        # the current file and state
    if not conn.enabled:
        return {"state": "paused", "messages": 0}
    if not _lease(conn, owner, max(60, conn.settings.poll_seconds * 2)):
        return {"state": "busy", "messages": 0}
    try:
        return _pass(conn)
    finally:
        _release(conn, owner)       # one pass at a time; between passes anyone may read


def _release(conn, owner: str) -> None:
    def release(state: dict):
        lease = state.get("email_poll_lease") or {}
        return ({"email_poll_lease": {}} if lease.get("owner") == owner else None), None

    store.swap_connection_state(conn.hub_dir, conn.hub, conn.id, release)


def _pass(conn) -> dict:
    cursor = (conn.state or {}).get("email_cursor")
    try:
        messages, new_cursor = transports.for_conn(conn).fetch_new(conn, cursor)
    except ChannelError as exc:
        store.set_connection_state(conn.hub_dir, conn.hub, conn.id, email_last_error=exc.code,
                                   email_last_poll=time.time())
        raise
    seen = []
    for m in messages:
        out = process(conn, m)
        seen.append(out)
    extra = {}
    if (new_cursor or {}).get("gap"):
        # Recovery could not cover everything: say so, never pretend.
        extra["email_gap"] = {"messages": new_cursor["gap"], "at": time.time()}
        log.warning("channel %s: %s older messages were not re-read after a cursor reset", conn.id,
                    new_cursor["gap"])
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id, email_cursor=new_cursor, email_last_poll=time.time(),
                               email_last_error=None, **extra)
    return {"state": "started" if cursor is None else "ok", "messages": len(messages), "deliveries": seen}


def _keep_source(conn, provider_id: str, raw: bytes) -> str | None:
    from ...intake import service as intake

    try:
        stored = intake.files_for(conn.hub_dir).register_bytes(
            f"intake:{conn.id}", f"{hashlib.sha256(provider_id.encode()).hexdigest()[:16]}.eml", "message/rfc822",
            raw, origin="intake", check_type=False)
    except Exception as exc:  # noqa: BLE001 - nothing is admitted without its source
        log.warning("channel %s: could not keep the source of %s", conn.id, provider_id)
        raise ChannelError("source_not_stored", "The message source could not be stored (storage full or "
                           "unavailable). It will be read again on the next pass.", status=503) from exc
    return stored["file_id"] if isinstance(stored, dict) else str(stored)


def build_event(conn, provider_id: str, raw: bytes, *, thread_id=None, too_large=False, dnsfunc=None,
                source_file_id=None) -> InboundEvent:
    cfg = conn.settings
    if too_large:
        parsed = mime.Parsed(None, None, "", "", "", None, [], "", problems=["message_too_large"],
                             raw_sha256="sha256:" + hashlib.sha256(provider_id.encode()).hexdigest())
    else:
        parsed = mime.parse(raw)
    trust = mime.sender_trust(raw, parsed, mailbox=cfg.mailbox, allowed=cfg.sender_policy.allowed,
                              authentication=cfg.sender_policy.authentication, dnsfunc=dnsfunc)
    sender = parsed.from_addr or "unknown"
    root = mime.thread_root(parsed) or f"<{provider_id}@hubzoid.local>"
    thread_key = "email:" + hashlib.sha256(f"{sender}\n{root}".encode()).hexdigest()[:32]
    origin = Origin("email", cfg.mailbox.lower(), thread_key,
                    {"reply_to": sender, "subject": parsed.subject, "root_message_id": root,
                     "provider_thread_id": thread_id})
    key = parsed.message_id or parsed.raw_sha256
    ev = InboundEvent(
        semantic_key="email:" + hashlib.sha256(key.encode()).hexdigest(), kind="email", origin=origin,
        actor=ExternalActor(sender, parsed.from_name[:200], False, {"trust": trust.get("reason")}),
        text=(f"Subject: {parsed.subject}\n\n" if parsed.subject else "") + parsed.text, trigger=True,
        message_id=parsed.message_id or provider_id, files=list(parsed.attachments),
        data={"trust": trust, "subject": parsed.subject, "message_id": parsed.message_id,
              "references": (parsed.references + ([parsed.message_id] if parsed.message_id else []))[-20:],
              "source_file_id": source_file_id, "provider_id": provider_id, "problems": parsed.problems,
              "provider_data": {"date": parsed.date, "provider_thread_id": thread_id}})
    if trust["decision"] == "rejected":
        ev.ignore_reason = trust["reason"]     # own mail and automatic replies are recorded, never work
    return ev


def process(conn, message: dict, *, dnsfunc=None) -> dict:
    raw = message.get("raw") or b""
    source = _keep_source(conn, message["id"], raw) if raw else None
    ev = build_event(conn, message["id"], raw, thread_id=message.get("thread_id"),
                     too_large=bool(message.get("too_large")), dnsfunc=dnsfunc, source_file_id=source)
    verified = Verified(delivery_id=message["id"], workspace_id=conn.settings.mailbox.lower(),
                        method=f"{conn.settings.transport}_mailbox", body=raw)
    out = admission.admit(conn, verified, [ev], raw=raw or message["id"].encode())
    return {"id": message["id"], "status": out.status, "trust": ev.data["trust"]["decision"]}
