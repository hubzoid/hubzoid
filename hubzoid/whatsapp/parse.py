"""Turn a Meta WhatsApp webhook payload into `InboundMessage` objects.

Meta batches under ``entry[].changes[].value``; ``value.messages`` holds inbound
messages and ``value.contacts`` their display names. Delivery/read receipts
arrive as ``value.statuses`` (no ``messages``) and yield nothing. Only text and
quick-reply/button messages carry text we can answer; other media types are
skipped for now. Defensive throughout — a malformed payload yields ``[]``,
never an exception.
"""
from __future__ import annotations

from ..inbound.message import InboundMessage

SURFACE = "whatsapp"


def parse_messages(payload: dict) -> "list[InboundMessage]":
    out: "list[InboundMessage]" = []
    if not isinstance(payload, dict):
        return out
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            names = _names_by_wa_id(value.get("contacts"))
            for msg in value.get("messages") or []:
                parsed = _parse_one(msg, names)
                if parsed is not None:
                    out.append(parsed)
    return out


def _names_by_wa_id(contacts) -> "dict[str, str]":
    names: "dict[str, str]" = {}
    for c in contacts or []:
        if not isinstance(c, dict):
            continue
        wa_id = c.get("wa_id")
        name = (c.get("profile") or {}).get("name") if isinstance(c.get("profile"), dict) else None
        if wa_id and name:
            names[wa_id] = name
    return names


def _parse_one(msg, names) -> "InboundMessage | None":
    if not isinstance(msg, dict):
        return None
    mid = msg.get("id")
    sender = msg.get("from")
    if not mid or not sender:
        return None
    text = _extract_text(msg)
    if text is None:
        return None  # unsupported type (media, etc.) — nothing to answer
    return InboundMessage(
        id=mid, surface=SURFACE, handle=sender, text=text, name=names.get(sender),
    )


def _clean(value) -> "str | None":
    """A non-empty stripped string, or None (also None for non-string input)."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _sub(msg: dict, key: str) -> dict:
    v = msg.get(key)
    return v if isinstance(v, dict) else {}


def _extract_text(msg: dict) -> "str | None":
    mtype = msg.get("type")
    if mtype == "text":
        return _clean(_sub(msg, "text").get("body"))
    if mtype == "button":
        return _clean(_sub(msg, "button").get("text"))
    if mtype == "interactive":
        inter = _sub(msg, "interactive")
        for key in ("button_reply", "list_reply"):
            reply = inter.get(key)
            if isinstance(reply, dict):
                title = _clean(reply.get("title"))
                if title:
                    return title
        return None
    return None
