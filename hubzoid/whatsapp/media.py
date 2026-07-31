"""Download WhatsApp media (Meta's two-step: media id -> signed URL -> bytes).

An inbound media message carries only a media *id*. Resolving it to bytes is
``GET /{media-id}`` (returns a short-lived ``url``) then ``GET`` that url, both
bearer-authenticated. Failures return None so a bad attachment never sinks the
turn. The http client is injectable for tests.
"""
from __future__ import annotations

import logging

import httpx

from ..inbound.message import MediaRef

GRAPH_VERSION = "v22.0"
_FETCH_TIMEOUT = 30.0

log = logging.getLogger("hubzoid.inbound")


def fetch(ref: MediaRef, *, token: str, http: "httpx.Client | None" = None) -> "bytes | None":
    """Return the bytes for one WhatsApp MediaRef, or None on any failure."""
    client = http or httpx.Client(timeout=_FETCH_TIMEOUT)
    owns = http is None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        meta = client.get(
            f"https://graph.facebook.com/{GRAPH_VERSION}/{ref.key}", headers=headers,
        )
        if meta.status_code != 200:
            log.warning("whatsapp media %s lookup returned %s", ref.key, meta.status_code)
            return None
        url = (meta.json() or {}).get("url")
        if not url:
            log.warning("whatsapp media %s has no download url", ref.key)
            return None
        blob = client.get(url, headers=headers)
        if blob.status_code != 200 or not blob.content:
            log.warning("whatsapp media %s download returned %s", ref.key, blob.status_code)
            return None
        return blob.content
    except Exception:  # noqa: BLE001 — a download hiccup drops the file, not the turn
        log.exception("whatsapp media fetch failed for %s", ref.key)
        return None
    finally:
        if owns:
            client.close()
