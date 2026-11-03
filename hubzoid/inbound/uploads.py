"""The one way a channel hands over a file it received.

Slack, WhatsApp and Telegram each download the bytes from their provider, then
`push_upload` sends them to the bridge's internal upload route (`POST /v1/files`,
with the bridge key; the same `attachments.save` as `POST /api/chat/files`) and
returns the stored file id. The channel puts that id
in the message as a file part (`file_part`), and the bridge attaches it to the
chat (`hubzoid.attachments`): images are shown to the model directly, any other
file gets a note and is read with `read_upload`. Each channel runs as its own
process, so only the bridge writes the file store.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("hubzoid.inbound")

# Uploads are short POSTs; bound them so a stalled bridge can't hang a worker.
_UPLOAD_TIMEOUT = 30.0


def upload_url(bridge_url: str) -> str:
    """The bridge's `POST /v1/files`. `bridge_url` is the `/v1` base (as
    dispatch uses); never a naive rstrip('/v1'), which would also eat a port
    ending in '1'."""
    base = bridge_url.rstrip("/")
    return f"{base}/files" if base.endswith("/v1") else f"{base}/v1/files"


def file_part(file_id: str) -> dict:
    """The message part that names a stored file (OpenAI's file content part)."""
    return {"type": "file", "file": {"file_id": file_id}}


def with_files(text: str, file_ids: "list[str]"):
    """A user message's content: the text alone, or text plus file parts."""
    if not file_ids:
        return text
    return ([{"type": "text", "text": text}] if text else []) + [file_part(f) for f in file_ids]


def push_upload(
    *,
    http: httpx.Client,
    bridge_url: str,
    api_key: str,
    name: str,
    mime: str,
    content: bytes,
    max_upload_bytes: int,
) -> "str | None":
    """Upload one attachment to the bridge and return its file id, or None.

    Oversized files and bridge errors are logged and skipped (return None), never
    raised — one bad attachment can't sink the turn.
    """
    if len(content) > max_upload_bytes:
        log.warning("inbound: attachment %s (%s bytes) exceeds cap (%s); skipping",
                    name, len(content), max_upload_bytes)
        return None
    try:
        resp = http.post(
            upload_url(bridge_url),
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (name, content, mime or "application/octet-stream")},
        )
    except Exception:  # noqa: BLE001 — network hiccup drops the file, not the turn
        log.exception("inbound: bridge upload failed for %s", name)
        return None
    if resp.status_code != 201:
        log.warning("inbound: bridge upload for %s returned %s; skipping", name, resp.status_code)
        return None
    try:
        return str(resp.json()["file_id"])
    except Exception:  # noqa: BLE001
        log.warning("inbound: bridge upload for %s returned no file id; skipping", name)
        return None
