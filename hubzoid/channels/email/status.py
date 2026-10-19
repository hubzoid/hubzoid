"""What the Console and CLI show about one mailbox, and the draft rehearsal."""
from __future__ import annotations

import secrets
import time

from .. import config as config_lib
from .. import store


def status(conn) -> dict:
    state = conn.state or {}
    cursor = state.get("email_cursor")
    return {"transport": conn.settings.transport, "mailbox": conn.settings.mailbox, "enabled": conn.enabled,
            "started": cursor is not None, "last_poll": state.get("email_last_poll"),
            "last_error": state.get("email_last_error"), "draft_verified": bool(state.get("email_draft_verified")),
            "connected": conn.settings.transport == "imap" or bool(state.get("email_scopes")),
            "senders_allowed": len(conn.settings.sender_policy.allowed),
            "authentication": conn.settings.sender_policy.authentication}


def rehearse_draft(conn, *, actor: str) -> dict:
    """Save one real draft, addressed to the mailbox itself, and read it back.
    Nothing is sent. Afterwards `email.send: draft` is allowed for this mailbox."""
    from . import mime, transports

    domain = conn.settings.mailbox.rpartition("@")[2] or "hubzoid.local"
    msgid = f"<rehearsal-{secrets.token_hex(8)}@{domain}>"
    raw = mime.build_reply(mailbox=conn.settings.mailbox, to=conn.settings.mailbox,
                           subject="Hubzoid draft check (safe to delete)",
                           body="Hubzoid saved this draft to check it can. Nothing was sent. You can delete it.",
                           in_reply_to=None, references=[], message_id=msgid)
    draft_id = transports.for_conn(conn).save_draft(conn, raw, message_id=msgid)
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id, email_draft_verified=True,
                               email_draft_checked_at=time.time())
    config_lib.audit(conn.hub_dir, actor, "email_draft_rehearsed", conn.id)
    return {"draft_verified": True, "draft_id": draft_id, "message_id": msgid,
            "note": "A draft is in the mailbox now. Nothing was sent. Delete it when you like."}
