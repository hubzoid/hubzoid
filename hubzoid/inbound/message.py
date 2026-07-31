"""The common shape every inbound surface parses a message into.

The harness works entirely in terms of `InboundMessage`, so WhatsApp, Telegram,
and any future surface converge here. `id` is the surface's unique message id
(used for dedup), `handle` is the sender's surface-native id (phone / Telegram
id) that the identity resolver maps to an email.
"""
from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field

# Common surface mimes -> a clean, human file extension. `mimetypes.guess_extension`
# is inconsistent across platforms (e.g. `.jpe` for jpeg), so pin the ones the
# webhook surfaces actually deliver and fall back to the stdlib for the rest.
_EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "application/pdf": ".pdf",
}


def ext_for(mime: "str | None") -> str:
    """A leading-dot file extension for a mime, or "" when unknown."""
    if not mime:
        return ""
    m = mime.split(";")[0].strip().lower()
    return _EXT_BY_MIME.get(m) or (mimetypes.guess_extension(m) or "")


@dataclass(frozen=True)
class MediaRef:
    """One inbound attachment, surface-agnostic. `key` is whatever the surface's
    fetcher needs to download the bytes (a WhatsApp media id, a Telegram file id).
    `name` is the filename to store it under; `mime` is a best-effort hint."""

    key: str
    name: str
    mime: "str | None" = None


@dataclass(frozen=True)
class InboundMessage:
    id: str            # unique per surface -> dedup key
    surface: str       # "whatsapp" | "telegram" | …
    handle: str        # sender's surface-native id (phone / numeric id)
    text: str          # the message text (already extracted)
    name: str | None = None   # sender display name, if the surface provides it
    media: "tuple[MediaRef, ...]" = field(default_factory=tuple)  # attachments, if any
