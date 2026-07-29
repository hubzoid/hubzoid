"""Prove an inbound Telegram request came from Telegram.

Set a ``secret_token`` when calling ``setWebhook``; Telegram then sends it back
in the ``X-Telegram-Bot-Api-Secret-Token`` header on every POST. Compare in
constant time. Fail closed if either side is missing — a hub that configured no
secret cannot verify, so it rejects rather than trusting an open URL.
"""
from __future__ import annotations

import hmac


def verify_secret(header: "str | None", expected: "str | None") -> bool:
    """True iff the header equals the configured secret; fail-closed otherwise."""
    if not header or not expected:
        return False
    # Encode to bytes so a non-ASCII header can't raise TypeError (fail closed).
    return hmac.compare_digest(header.encode("utf-8"), expected.encode("utf-8"))
