"""Outbound WhatsApp via the Meta Cloud API (Graph API).

Two shapes: ``send_text`` for a free-text reply inside the 24-hour customer
window, and ``send_template`` for a business-initiated (proactive) message,
which Meta requires to be a pre-approved template. Bearer-token auth.
"""
from __future__ import annotations

from typing import Any

import httpx

GRAPH_VERSION = "v22.0"
_SEND_TIMEOUT = 30.0  # outbound sends are short; bound them so a stall can't hang a worker


def _url(phone_number_id: str) -> str:
    return f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_number_id}/messages"


def _post(url: str, token: str, payload: dict, http_client, timeout) -> dict:
    client = http_client or httpx.Client(timeout=timeout if timeout is not None else _SEND_TIMEOUT)
    owns = http_client is None
    try:
        r = client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
        r.raise_for_status()
        return r.json()
    finally:
        if owns:
            client.close()


def send_text(*, phone_number_id, token, to, text, http_client=None, timeout=None) -> dict:
    """Send a free-text WhatsApp message (valid only inside the 24-hour window)."""
    payload: "dict[str, Any]" = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    return _post(_url(phone_number_id), token, payload, http_client, timeout)


def send_template(
    *, phone_number_id, token, to, template, language="en",
    components=None, http_client=None, timeout=None,
) -> dict:
    """Send an approved template (the only shape allowed to open a conversation)."""
    tpl: "dict[str, Any]" = {"name": template, "language": {"code": language}}
    if components:
        tpl["components"] = components
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": tpl,
    }
    return _post(_url(phone_number_id), token, payload, http_client, timeout)


def mark_read(*, phone_number_id, token, message_id, typing=False,
              http_client=None, timeout=None) -> dict:
    """Mark an incoming message read (blue ticks). With ``typing=True`` also show
    a typing indicator — it lasts ~25s or until we send the reply. One call does
    both. WhatsApp has no message editing, so this is the only in-progress cue
    available (there is no 'online'/presence API)."""
    payload: "dict[str, Any]" = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    if typing:
        payload["typing_indicator"] = {"type": "text"}
    return _post(_url(phone_number_id), token, payload, http_client, timeout)
