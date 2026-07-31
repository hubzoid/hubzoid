"""Download Telegram media (getFile -> file_path -> bytes).

A message references a file by ``file_id``; ``getFile`` resolves it to a
``file_path`` under ``https://api.telegram.org/file/bot<token>/<path>``, which we
then GET for the bytes. Failures return None so a bad attachment never sinks the
turn. The http client is injectable for tests.
"""
from __future__ import annotations

import logging

import httpx

from ..inbound.message import MediaRef

_FETCH_TIMEOUT = 30.0

log = logging.getLogger("hubzoid.inbound")


def fetch(ref: MediaRef, *, token: str, http: "httpx.Client | None" = None) -> "bytes | None":
    """Return the bytes for one Telegram MediaRef, or None on any failure."""
    client = http or httpx.Client(timeout=_FETCH_TIMEOUT)
    owns = http is None
    try:
        info = client.get(
            f"https://api.telegram.org/bot{token}/getFile", params={"file_id": ref.key},
        )
        body = info.json() if info.status_code == 200 else {}
        if not body.get("ok"):
            log.warning("telegram getFile for %s failed: %s", ref.key, info.status_code)
            return None
        file_path = (body.get("result") or {}).get("file_path")
        if not file_path:
            log.warning("telegram getFile for %s returned no file_path", ref.key)
            return None
        blob = client.get(f"https://api.telegram.org/file/bot{token}/{file_path}")
        if blob.status_code != 200 or not blob.content:
            log.warning("telegram file download for %s returned %s", ref.key, blob.status_code)
            return None
        return blob.content
    except Exception:  # noqa: BLE001 — a download hiccup drops the file, not the turn
        log.exception("telegram media fetch failed for %s", ref.key)
        return None
    finally:
        if owns:
            client.close()
