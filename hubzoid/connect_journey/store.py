"""Server-side state for connection journeys (`hz_connect_states`).

One row per "connect my <app>" request, in the deployment's shared operational
database, so any bridge can serve the link page and the originating hub's
inbound process can send the chat confirmation.

Every state change is a compare-and-set on the current status, so two pages,
a poller and a tool racing on one journey can never both win a transition.
Rows hold no secrets. ``provider_ref`` names the provider-side object (an Open
WebUI tool-server id, or a Composio connected-account id and its hosted link)
and is never shown to the model or sent to a chat.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from pathlib import Path

from sqlalchemy import text

from ..access.identity import normalize

log = logging.getLogger("hubzoid.connect")

ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
OPEN = ("pending", "started")
TERMINAL = ("connected", "cancelled", "failed", "expired", "superseded")
# The confirmation's "Reply YES" offer stays open this long after it was sent.
OFFER_SECONDS = 600
_CONTINUATION_MAX = 4000

_COLUMNS = ("id", "created", "expires", "hub", "subject", "surface", "chat_id", "handle",
            "app", "provider", "provider_ref", "status", "started", "finished",
            "continuation", "continuation_status", "notified")
_SELECT = "SELECT " + ", ".join(_COLUMNS) + " FROM hz_connect_states"


def _engine(hub_dir):
    from .. import db, migrations

    engine = db.operational_engine(Path(hub_dir))
    migrations.upgrade(engine, "operational")
    return engine


def _row(r) -> dict:
    return dict(zip(_COLUMNS, r))


def new_id() -> str:
    """An unguessable journey id (32 URL-safe characters, 192 bits)."""
    return secrets.token_urlsafe(24)


def valid_id(jid: str) -> bool:
    return bool(jid) and bool(ID_RE.match(jid))


def get(hub_dir, jid: str) -> dict | None:
    if not valid_id(jid):
        return None
    with _engine(hub_dir).connect() as c:
        r = c.execute(text(_SELECT + " WHERE id = :id"), {"id": jid}).fetchone()
    return _row(r) if r else None


def create(hub_dir, *, jid: str, hub: str, subject: str, surface: str, chat_id: str | None,
           handle: str | None, app: str, provider: str, provider_ref: str | None,
           ttl: float, now: float | None = None) -> tuple[dict, list[dict]]:
    """Insert a pending journey and supersede the subject's older open journeys
    for the same app, in one transaction. Returns (new row, superseded rows)."""
    now = time.time() if now is None else now
    subject = normalize(subject)
    row = {
        "id": jid, "created": now, "expires": now + ttl, "hub": normalize(hub),
        "subject": subject, "surface": surface, "chat_id": chat_id, "handle": handle,
        "app": app, "provider": provider, "provider_ref": provider_ref,
        "status": "pending", "started": None, "finished": None, "continuation": None,
        "continuation_status": "none", "notified": None,
    }
    with _engine(hub_dir).begin() as c:
        older = [_row(r) for r in c.execute(
            text(_SELECT + " WHERE subject = :s AND app = :a AND status IN ('pending', 'started')"),
            {"s": subject, "a": app}).fetchall()]
        if older:
            c.execute(text(
                "UPDATE hz_connect_states SET status = 'superseded', finished = :now, "
                "continuation = NULL, notified = COALESCE(notified, :now) "
                "WHERE subject = :s AND app = :a AND status IN ('pending', 'started')"),
                {"now": now, "s": subject, "a": app})
        c.execute(text(
            "INSERT INTO hz_connect_states (" + ", ".join(_COLUMNS) + ") VALUES ("
            + ", ".join(f":{k}" for k in _COLUMNS) + ")"), row)
    return row, older


def mark_started(hub_dir, jid: str, *, ttl: float, now: float | None = None) -> bool:
    """pending/started -> started, before expiry. The first start time is kept
    (a connection made after it counts), and the window is renewed so a slow
    consent screen does not expire mid-flow."""
    now = time.time() if now is None else now
    with _engine(hub_dir).begin() as c:
        res = c.execute(text(
            "UPDATE hz_connect_states SET status = 'started', "
            "started = COALESCE(started, :now), expires = :exp "
            "WHERE id = :id AND status IN ('pending', 'started') AND expires > :now"),
            {"id": jid, "now": now, "exp": now + ttl})
    return res.rowcount == 1


def transition(hub_dir, jid: str, *, frm: tuple[str, ...], to: str,
               now: float | None = None) -> bool:
    """Compare-and-set the status. Terminal states stamp ``finished``."""
    now = time.time() if now is None else now
    params = {"id": jid, "to": to, "now": now}
    params.update({f"f{i}": s for i, s in enumerate(frm)})
    placeholders = ", ".join(f":f{i}" for i in range(len(frm)))
    extra = ", finished = :now" if to in TERMINAL else ""
    if to in ("cancelled", "failed", "expired", "superseded"):
        extra += ", continuation = NULL"
    with _engine(hub_dir).begin() as c:
        res = c.execute(text(
            f"UPDATE hz_connect_states SET status = :to{extra} "
            f"WHERE id = :id AND status IN ({placeholders})"), params)
    return res.rowcount == 1


def open_for(hub_dir, *, subject: str, app: str) -> list[dict]:
    with _engine(hub_dir).connect() as c:
        rows = c.execute(text(
            _SELECT + " WHERE subject = :s AND app = :a AND status IN ('pending', 'started')"),
            {"s": normalize(subject), "a": app}).fetchall()
    return [_row(r) for r in rows]


def created_since(hub_dir, *, subject: str, surface: str, chat_id: str,
                  since: float) -> list[dict]:
    """Journeys this chat's turn created (newest first)."""
    with _engine(hub_dir).connect() as c:
        rows = c.execute(text(
            _SELECT + " WHERE subject = :s AND surface = :sf AND chat_id = :c "
            "AND created >= :since ORDER BY created DESC"),
            {"s": normalize(subject), "sf": surface, "c": chat_id, "since": since}).fetchall()
    return [_row(r) for r in rows]


def outbox(hub_dir, *, hub: str, surface: str, since: float) -> list[dict]:
    """Journeys from this hub and surface whose outcome has not been handled."""
    with _engine(hub_dir).connect() as c:
        rows = c.execute(text(
            _SELECT + " WHERE hub = :h AND surface = :sf AND notified IS NULL "
            "AND created >= :since ORDER BY created"),
            {"h": normalize(hub), "sf": surface, "since": since}).fetchall()
    return [_row(r) for r in rows]


def claim_notify(hub_dir, jid: str, *, now: float | None = None) -> bool:
    """Atomically mark a journey's outcome as handled. Only the caller that
    wins may send the chat message, so it is sent at most once."""
    now = time.time() if now is None else now
    with _engine(hub_dir).begin() as c:
        res = c.execute(text(
            "UPDATE hz_connect_states SET notified = :now WHERE id = :id AND notified IS NULL"),
            {"id": jid, "now": now})
    return res.rowcount == 1


# ---- continuation ----------------------------------------------------------
def attach_continuation(hub_dir, *, subject: str, surface: str, chat_id: str,
                        since: float, text_: str) -> bool:
    """Attach a turn's verbatim user text to the newest open journey that turn
    created in this chat. The model never supplies it."""
    body = (text_ or "").strip()
    if not body:
        return False
    body = body[:_CONTINUATION_MAX]
    with _engine(hub_dir).begin() as c:
        r = c.execute(text(
            "SELECT id FROM hz_connect_states WHERE subject = :s AND surface = :sf "
            "AND chat_id = :c AND created >= :since AND status IN ('pending', 'started') "
            "AND continuation IS NULL AND continuation_status = 'none' "
            "ORDER BY created DESC LIMIT 1"),
            {"s": normalize(subject), "sf": surface, "c": chat_id, "since": since}).fetchone()
        if not r:
            return False
        res = c.execute(text(
            "UPDATE hz_connect_states SET continuation = :t WHERE id = :id "
            "AND continuation IS NULL AND continuation_status = 'none'"),
            {"t": body, "id": r[0]})
    return res.rowcount == 1


def offer_continuation(hub_dir, jid: str) -> str | None:
    """Mark an attached continuation as offered. Returns its text, or None."""
    with _engine(hub_dir).begin() as c:
        r = c.execute(text(
            "SELECT continuation FROM hz_connect_states WHERE id = :id "
            "AND status = 'connected' AND continuation IS NOT NULL "
            "AND continuation_status = 'none'"), {"id": jid}).fetchone()
        if not r:
            return None
        res = c.execute(text(
            "UPDATE hz_connect_states SET continuation_status = 'offered' "
            "WHERE id = :id AND continuation_status = 'none'"), {"id": jid})
    return r[0] if res.rowcount == 1 else None


def take_continuation(hub_dir, *, subject: str, surface: str, chat_id: str,
                      now: float | None = None) -> str | None:
    """Claim the offered continuation for this sender and chat, once.

    Atomic single use: the offered -> used compare-and-set has exactly one
    winner, and the stored text is cleared as it is taken."""
    now = time.time() if now is None else now
    with _engine(hub_dir).begin() as c:
        r = c.execute(text(
            "SELECT id, continuation FROM hz_connect_states WHERE subject = :s "
            "AND surface = :sf AND chat_id = :c AND continuation_status = 'offered' "
            "AND notified >= :cutoff ORDER BY notified DESC LIMIT 1"),
            {"s": normalize(subject), "sf": surface, "c": chat_id,
             "cutoff": now - OFFER_SECONDS}).fetchone()
        if not r:
            return None
        res = c.execute(text(
            "UPDATE hz_connect_states SET continuation_status = 'used', continuation = NULL "
            "WHERE id = :id AND continuation_status = 'offered'"), {"id": r[0]})
    return r[1] if res.rowcount == 1 else None


def decline_continuation(hub_dir, *, subject: str, surface: str, chat_id: str) -> int:
    """Any other reply clears an offered continuation."""
    with _engine(hub_dir).begin() as c:
        res = c.execute(text(
            "UPDATE hz_connect_states SET continuation_status = 'declined', continuation = NULL "
            "WHERE subject = :s AND surface = :sf AND chat_id = :c "
            "AND continuation_status = 'offered'"),
            {"s": normalize(subject), "sf": surface, "c": chat_id})
    return res.rowcount


def expire_offers(hub_dir, *, hub: str, now: float | None = None) -> int:
    """Close offers older than the window and drop continuations of journeys
    that can no longer connect, so no request text lingers."""
    now = time.time() if now is None else now
    with _engine(hub_dir).begin() as c:
        res = c.execute(text(
            "UPDATE hz_connect_states SET continuation_status = 'expired', continuation = NULL "
            "WHERE hub = :h AND continuation_status = 'offered' AND notified < :cutoff"),
            {"h": normalize(hub), "cutoff": now - OFFER_SECONDS})
        c.execute(text(
            "UPDATE hz_connect_states SET continuation = NULL WHERE hub = :h "
            "AND continuation IS NOT NULL AND continuation_status = 'none' "
            "AND status NOT IN ('pending', 'started', 'connected')"),
            {"h": normalize(hub)})
    return res.rowcount


# ---- audit -----------------------------------------------------------------
def audit(hub_dir, *, hub: str, subject: str | None, surface: str | None, app: str,
          decision: str, reason: str) -> bool:
    """One row in the access decision log (`hz_access_decisions`) for a
    connection outcome, under the journey's own hub. Never raises."""
    try:
        with _engine(hub_dir).begin() as c:
            c.execute(text(
                "INSERT INTO hz_access_decisions (ts, hub, subject, surface, tool, decision, reason) "
                "VALUES (:t, :h, :s, :sf, :tl, :d, :r)"),
                {"t": time.time(), "h": normalize(hub), "s": normalize(subject or "") or "anonymous",
                 "sf": surface or "", "tl": f"connect:{app}", "d": decision, "r": reason})
        return True
    except Exception:  # noqa: BLE001
        log.error("connect: could not record the %s outcome for %s", reason, app, exc_info=True)
        return False
