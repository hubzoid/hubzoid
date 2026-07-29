"""Contact-share enrollment for Telegram — the fixed, LLM-free handshake.

Telegram never reveals a phone on a normal message, only when the user taps a
"share my number" button. That single tap yields both their phone and their
numeric id, so we bind the id to the roster row keyed on that phone. From then
on a message from that numeric id resolves via the stored phone.

Security: we only accept the sender's OWN contact (``user_id`` must equal the
sender), so nobody can enroll by forwarding someone else's number.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..inbound.normalize import normalize_phone


@dataclass(frozen=True)
class EnrollResult:
    status: str            # "verified" | "not_registered" | "not_own_contact"
    email: "str | None" = None


class Bindings:
    """Persistent numeric-id -> phone store (one small file per id)."""

    def __init__(self, directory) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def bind(self, telegram_id: str, phone: str) -> None:
        path = self.dir / _name(telegram_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(normalize_phone(phone), encoding="utf-8")
        tmp.replace(path)  # atomic: a crash mid-write can't leave a truncated binding

    def phone_for(self, telegram_id: str) -> "str | None":
        p = self.dir / _name(telegram_id)
        if not p.is_file():
            return None
        return (p.read_text(encoding="utf-8") or "").strip() or None


def enroll_contact(*, handle, contact_user_id, contact_phone, resolver, bindings) -> EnrollResult:
    """Bind the sender's shared number to their roster row, or explain why not."""
    if contact_user_id is None or str(contact_user_id) != str(handle):
        return EnrollResult("not_own_contact")
    phone = normalize_phone(contact_phone)
    identity = resolver("telegram", phone) if resolver is not None else None
    if not identity or not identity.get("email"):
        return EnrollResult("not_registered")
    bindings.bind(handle, phone)
    return EnrollResult("verified", email=identity["email"])


def resolve_telegram(handle, resolver, bindings) -> "dict | None":
    """Resolve a Telegram numeric id to its identity via the stored phone binding."""
    phone = bindings.phone_for(handle)
    if not phone or resolver is None:
        return None
    return resolver("telegram", phone)


def _name(telegram_id: str) -> str:
    return hashlib.sha256(str(telegram_id).encode("utf-8")).hexdigest()
