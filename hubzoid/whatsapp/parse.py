"""Turn a Meta WhatsApp webhook payload into `InboundMessage` objects.

Meta batches under ``entry[].changes[].value``; ``value.messages`` holds inbound
messages and ``value.contacts`` their display names. Delivery/read receipts
arrive as ``value.statuses`` (no ``messages``) and yield nothing. Text and
quick-reply/button messages carry answerable text; media messages (image,
document, audio, video, voice, sticker) surface as `MediaRef`s the harness
downloads and attaches, with any caption as the text. Defensive throughout — a
malformed payload yields ``[]``, never an exception.
"""
from __future__ import annotations

from ..inbound.message import InboundMessage, MediaRef, ext_for

SURFACE = "whatsapp"

# Media message types. `document` carries a filename; the rest we name from the
# media id + a mime-derived extension so distinct attachments never collide.
_MEDIA_TYPES = ("image", "document", "audio", "video", "voice", "sticker")
# Types that can carry a caption alongside the media.
_CAPTION_TYPES = ("image", "document", "video")


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
    media = _extract_media(msg)
    text = _extract_text(msg)
    if text is None:
        text = _extract_caption(msg) if media else None
    if text is None and not media:
        return None  # unsupported type with nothing to answer
    return InboundMessage(
        id=mid, surface=SURFACE, handle=sender, text=text or "",
        name=names.get(sender), media=tuple(media),
    )


def _extract_media(msg: dict) -> "list[MediaRef]":
    refs: "list[MediaRef]" = []
    for mtype in _MEDIA_TYPES:
        obj = _sub(msg, mtype)
        media_id = obj.get("id")
        if not media_id:
            continue
        mime = _clean(obj.get("mime_type"))
        name = _clean(obj.get("filename")) or f"{mtype}-{media_id}{ext_for(mime)}"
        refs.append(MediaRef(key=media_id, name=name, mime=mime))
    return refs


def _extract_caption(msg: dict) -> "str | None":
    for mtype in _CAPTION_TYPES:
        caption = _clean(_sub(msg, mtype).get("caption"))
        if caption:
            return caption
    return None


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
