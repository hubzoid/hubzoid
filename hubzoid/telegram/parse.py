"""Parse and classify a Telegram Update.

Three kinds matter: ``text`` (a normal message -> dispatch to the agent),
``start`` (the /start command -> send the verify prompt), and ``contact`` (the
user shared their number -> enrollment binds their numeric id to a roster row).
Everything else is ``other`` (ignored). Non-message updates (callbacks, edits)
parse to ``None``. Defensive: a malformed update yields ``None``.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..inbound.message import InboundMessage

SURFACE = "telegram"


@dataclass(frozen=True)
class TgUpdate:
    update_id: str
    kind: str                      # "text" | "start" | "contact" | "other"
    handle: str                    # str(from.id) — the numeric user id
    text: str = ""
    name: "str | None" = None
    language_code: "str | None" = None
    contact_phone: "str | None" = None
    contact_user_id: "str | None" = None


def parse_update(update: dict) -> "TgUpdate | None":
    if not isinstance(update, dict):
        return None
    update_id = update.get("update_id")
    message = update.get("message")
    if update_id is None or not isinstance(message, dict):
        return None
    sender = message.get("from")
    if not isinstance(sender, dict) or sender.get("id") is None:
        return None

    handle = str(sender["id"])
    name = sender.get("first_name")
    language_code = sender.get("language_code")
    common = dict(
        update_id=str(update_id), handle=handle, name=name, language_code=language_code,
    )

    contact = message.get("contact")
    if isinstance(contact, dict) and contact.get("phone_number"):
        uid = contact.get("user_id")
        return TgUpdate(
            kind="contact",
            contact_phone=contact.get("phone_number"),
            contact_user_id=str(uid) if uid is not None else None,
            **common,
        )

    text = message.get("text")
    if isinstance(text, str) and text.strip():
        if text.strip().startswith("/start"):
            return TgUpdate(kind="start", text=text.strip(), **common)
        return TgUpdate(kind="text", text=text.strip(), **common)

    return TgUpdate(kind="other", **common)


def to_inbound(p: TgUpdate) -> InboundMessage:
    """A `text` update -> the shared InboundMessage the harness dispatches."""
    return InboundMessage(
        id=p.update_id, surface=SURFACE, handle=p.handle, text=p.text, name=p.name,
    )
