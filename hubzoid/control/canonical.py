"""Canonical JSON and content digests.

A digest binds an approval to exact content: ``sha256:`` plus the lowercase
hex hash of the canonical JSON (sorted keys, compact separators, UTF-8, string
content kept exactly). The same document always gives the same digest on every
machine, so a review recorded in one place can be checked in another.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    """The content digest of a JSON-shaped value."""
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def request_hash(value: Any) -> str:
    """The hash an idempotency record keeps of a request body."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
