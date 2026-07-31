"""Parse and classify a Telegram Update.

Three kinds matter: ``text`` (a normal message -> dispatch to the agent),
``start`` (the /start command -> send the verify prompt), and ``contact`` (the
user shared their number -> enrollment binds their numeric id to a roster row).
A message carrying media (photo, document, voice, video, audio) is also ``text``:
the media surfaces as `MediaRef`s the harness downloads, with any caption as the
text. Everything else is ``other`` (ignored). Non-message updates (callbacks,
edits) parse to ``None``. Defensive: a malformed update yields ``None``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..inbound.message import InboundMessage, MediaRef, ext_for

SURFACE = "telegram"

# Object-typed media (each is a single dict with a file_id). `photo` is handled
# separately because it arrives as an array of sizes.
_MEDIA_KEYS = (
    ("document", None), ("voice", "audio/ogg"), ("video", "video/mp4"),
    ("audio", "audio/mpeg"), ("video_note", "video/mp4"),
)


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
    media: "tuple[MediaRef, ...]" = field(default_factory=tuple)


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

    media = _extract_media(message)
    text = message.get("text")
    caption = message.get("caption")
    body = ""
    if isinstance(text, str) and text.strip():
        body = text.strip()
    elif isinstance(caption, str) and caption.strip():
        body = caption.strip()

    # /start is a plain command (never carries media); keep its special reply.
    if body.startswith("/start") and not media:
        return TgUpdate(kind="start", text=body, **common)
    if media or body:
        return TgUpdate(kind="text", text=body, media=tuple(media), **common)

    return TgUpdate(kind="other", **common)


def _extract_media(message: dict) -> "list[MediaRef]":
    refs: "list[MediaRef]" = []
    photos = message.get("photo")
    if isinstance(photos, list):
        sizes = [p for p in photos if isinstance(p, dict) and p.get("file_id")]
        if sizes:
            largest = sizes[-1]  # Telegram orders smallest -> largest
            uid = largest.get("file_unique_id") or largest["file_id"]
            refs.append(MediaRef(key=largest["file_id"], name=f"photo-{uid}.jpg",
                                 mime="image/jpeg"))
    for key, default_mime in _MEDIA_KEYS:
        obj = message.get(key)
        if not isinstance(obj, dict) or not obj.get("file_id"):
            continue
        mime = obj.get("mime_type") or default_mime
        name = obj.get("file_name")
        if not isinstance(name, str) or not name.strip():
            uid = obj.get("file_unique_id") or obj["file_id"]
            name = f"{key}-{uid}{ext_for(mime)}"
        refs.append(MediaRef(key=obj["file_id"], name=name, mime=mime))
    return refs


def to_inbound(p: TgUpdate) -> InboundMessage:
    """A `text` update -> the shared InboundMessage the harness dispatches."""
    return InboundMessage(
        id=p.update_id, surface=SURFACE, handle=p.handle, text=p.text, name=p.name,
        media=p.media,
    )
