"""Prove an inbound WhatsApp request really came from Meta.

Two checks, both required before anything else runs:

  * GET handshake (once, at webhook setup): Meta calls with
    ``hub.mode=subscribe``, ``hub.verify_token=<your token>`` and
    ``hub.challenge=<n>``; we echo the challenge only if the token matches.
  * POST signature (every message): Meta sends
    ``X-Hub-Signature-256: sha256=<hex>`` where the hex is
    HMAC-SHA256(app_secret, RAW request body). The body must be the exact
    bytes received — re-serializing JSON would change the signature.
"""
from __future__ import annotations

import hashlib
import hmac

_PREFIX = "sha256="


def _ct_equal(a: str, b: str) -> bool:
    """Constant-time string compare that also survives non-ASCII input. A raw
    ``hmac.compare_digest(str, str)`` raises TypeError on a byte > 0x7F; encoding
    to bytes makes a hostile header fail closed instead of raising."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_challenge(params: "dict[str, str]", expected_token: str) -> "str | None":
    """Return the challenge string to echo, or None if the handshake is invalid."""
    if params.get("hub.mode") != "subscribe":
        return None
    provided = params.get("hub.verify_token")
    if not expected_token or not provided or not _ct_equal(provided, expected_token):
        return None
    return params.get("hub.challenge")


def verify_signature(raw_body: bytes, header: "str | None", app_secret: str) -> bool:
    """True iff `header` is a valid HMAC-SHA256 of `raw_body` under `app_secret`.

    Fails closed on a missing, malformed, or empty signature. Uses a constant-
    time compare so a wrong signature can't be timed byte by byte.
    """
    if not header or not app_secret or not header.startswith(_PREFIX):
        return False
    provided = header[len(_PREFIX):]
    if not provided:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return _ct_equal(provided, expected)
