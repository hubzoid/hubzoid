"""Outbound Telegram via the Bot API sendMessage, using HTML parse mode."""
from __future__ import annotations

from typing import Any

import httpx

_SEND_TIMEOUT = 30.0  # outbound sends are short; bound them so a stall can't hang a worker


def send_message(
    *, token, chat_id, text, parse_mode="HTML", reply_markup=None,
    http_client=None, timeout=None,
) -> dict:
    """Send a Telegram message. `chat_id` is the recipient's numeric id (as str).

    `reply_markup` (optional) attaches a keyboard — used by enrollment to show
    the one-tap "share my number" button.
    """
    payload: "dict[str, Any]" = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    client = http_client or httpx.Client(timeout=timeout if timeout is not None else _SEND_TIMEOUT)
    owns = http_client is None
    try:
        r = client.post(url, json=payload)
        r.raise_for_status()
        return r.json()
    finally:
        if owns:
            client.close()


def edit_message_text(*, token, chat_id, message_id, text, parse_mode="HTML",
                      http_client=None, timeout=None) -> dict:
    """Edit a message in place. Used for character-by-character streaming: send a
    placeholder, then edit it as tokens arrive (Telegram has no token stream)."""
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {
        "chat_id": chat_id, "message_id": message_id, "text": text,
        "parse_mode": parse_mode, "disable_web_page_preview": True,
    }
    client = http_client or httpx.Client(timeout=timeout if timeout is not None else _SEND_TIMEOUT)
    owns = http_client is None
    try:
        r = client.post(url, json=payload)
        r.raise_for_status()
        return r.json()
    finally:
        if owns:
            client.close()


def send_chat_action(*, token, chat_id, action="typing", http_client=None, timeout=None) -> dict:
    """Show a chat action (e.g. 'typing…') to the recipient. Best-effort; the
    indicator lasts ~5s on Telegram, enough to cover the think-before-reply gap."""
    url = f"https://api.telegram.org/bot{token}/sendChatAction"
    payload = {"chat_id": chat_id, "action": action}
    client = http_client or httpx.Client(timeout=timeout if timeout is not None else _SEND_TIMEOUT)
    owns = http_client is None
    try:
        r = client.post(url, json=payload)
        r.raise_for_status()
        return r.json()
    finally:
        if owns:
            client.close()
