"""The common shape every inbound surface parses a message into.

The harness works entirely in terms of `InboundMessage`, so WhatsApp, Telegram,
and any future surface converge here. `id` is the surface's unique message id
(used for dedup), `handle` is the sender's surface-native id (phone / Telegram
id) that the identity resolver maps to an email.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InboundMessage:
    id: str            # unique per surface -> dedup key
    surface: str       # "whatsapp" | "telegram" | …
    handle: str        # sender's surface-native id (phone / numeric id)
    text: str          # the message text (already extracted)
    name: str | None = None   # sender display name, if the surface provides it
