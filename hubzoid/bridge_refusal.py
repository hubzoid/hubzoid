"""What a 1.x channel listener tells a person when the bridge refuses or fails.

The legacy Slack app (``hubzoid.slack.adapter``) and the Telegram and WhatsApp
listener (``hubzoid.inbound``) call the bridge's ``/v1/chat/completions``. A
refusal there is an HTTP error whose text names the bridge's local address and,
for a 403, the signed-in email. Neither belongs in a chat, least of all in a
shared Slack thread. ``notice_for`` turns any failure into plain words: no URL,
no exception text, no bridge detail. Anything unexpected gets a short reference
that is also logged with the full error, so an administrator can find it.

Mastra keeps refusals generic for the same reason (it never echoes the host's
authorization message into a shared thread) while still telling the sender
something, so nobody waits for an answer that will not come.
"""
from __future__ import annotations

import logging
import secrets

log = logging.getLogger("hubzoid.bridge_refusal")

BLOCKED = "Your access to this agent is blocked. Ask an administrator."
NO_ACCESS = "I can't act for you here yet. Ask an administrator to give you access to this agent."
MISCONFIGURED = "This agent's {surface} connection is not set up correctly. Ask an administrator to check it."
BUSY = "I'm restarting or busy. Try again in a minute."
FAILED = "I could not answer right now. Try again shortly. (ref {ref})"

_TITLES = {"slack": "Slack", "telegram": "Telegram", "whatsapp": "WhatsApp"}


def _detail(response) -> str:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - a body that is not JSON says nothing we use
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("code")
    return str(detail or "")


def notice_for(exc: BaseException, *, surface: str = "slack") -> str:
    """The words a person sees when a call to the bridge failed with ``exc``."""
    import httpx

    title = _TITLES.get(surface.split("-", 1)[0], surface.title())
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        status = exc.response.status_code
        if status == 403:
            # Only the kind of refusal is read, never repeated: the bridge's
            # detail can name the signed-in email.
            return BLOCKED if "blocked" in _detail(exc.response).lower() else NO_ACCESS
        if status in (401, 404):
            log.warning("%s listener: the bridge answered %s; check the API key and bridge port", title, status)
            return MISCONFIGURED.format(surface=title)
        if status in (429, 502, 503, 504):
            return BUSY
    ref = secrets.token_hex(4)
    log.error("%s listener: the bridge call failed (ref %s)", title, ref, exc_info=exc)
    return FAILED.format(ref=ref)
