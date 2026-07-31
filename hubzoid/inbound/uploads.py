"""The generic inbound attachment layer.

Every surface that receives a file (WhatsApp media, a Telegram photo) converges
here: `push_upload` streams the bytes to the bridge's per-chat uploads route and
returns the text marker to stitch into the user turn. From there the file rides
the exact same pipeline the web and Slack already use — persisted to the chat's
uploads dir with a sidecar, then either shown to the model directly (images, via
`vision_inject`) or read on demand (`read_upload`). The surface-specific half is
only "download the bytes"; this module is everything after that, shared.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("hubzoid.inbound")

# Uploads are short POSTs; bound them so a stalled bridge can't hang a worker.
_UPLOAD_TIMEOUT = 30.0


def _uploads_url(bridge_url: str, chat_id: str, name: str) -> str:
    """The bridge's `/uploads/{chat_id}/{name}` endpoint. `bridge_url` is the
    `/v1` base (as dispatch uses); strip that suffix without touching the rest —
    a naive rstrip('/v1') would also eat a port ending in '1'."""
    base = bridge_url[:-3] if bridge_url.endswith("/v1") else bridge_url
    base = base.rstrip("/")
    return f"{base}/uploads/{chat_id}/{name}"


def _marker(name: str, mime: str, size: int) -> str:
    """The text reference the model sees. Images get the canonical `[Image: …]`
    that `vision_inject` expands to a real image block; everything else gets a
    read_upload note. Mirrors `server._persist_attachments`."""
    if (mime or "").lower().startswith("image/"):
        return f"[Image: {name}]  (attached image, shown to you directly)"
    return (
        f"[User attached file: {name} ({size} bytes, {mime}). "
        f"Read it with read_upload('{name}').]"
    )


def push_upload(
    *,
    http: httpx.Client,
    bridge_url: str,
    api_key: str,
    chat_id: str,
    name: str,
    mime: str,
    content: bytes,
    max_upload_bytes: int,
) -> "str | None":
    """POST one attachment to the bridge and return its marker, or None.

    Oversized files and bridge errors are logged and skipped (return None), never
    raised — one bad attachment can't sink the turn. `mime` becomes the upload's
    Content-Type, which the bridge records in the sidecar (so an image is
    classified as an image regardless of its filename).
    """
    if len(content) > max_upload_bytes:
        log.warning("inbound: attachment %s (%s bytes) exceeds cap (%s); skipping",
                    name, len(content), max_upload_bytes)
        return None
    try:
        resp = http.post(
            _uploads_url(bridge_url, chat_id, name),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": mime},
            content=content,
        )
    except Exception:  # noqa: BLE001 — network hiccup drops the file, not the turn
        log.exception("inbound: bridge upload POST failed for %s", name)
        return None
    if resp.status_code != 200:
        log.warning("inbound: bridge upload for %s returned %s; skipping",
                    name, resp.status_code)
        return None
    return _marker(name, mime, len(content))
