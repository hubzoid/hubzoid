"""Canonicalize a phone handle so an inbound message matches a roster row.

This is the one cleanup that silently breaks lookups if skipped: Meta sends a
`wa_id` like ``919800000001`` (country code + number, no plus), a Telegram
shared contact may carry ``+919800000001``, and an operator's roster cell may
read ``+91 98000-00001``. All three must reduce to the same key. The rule is
deliberately simple and surface-agnostic: keep the digits, drop everything
else. Operators are told to store numbers in full international form (country
code, no leading national ``0``).
"""
from __future__ import annotations

import re

_NON_DIGIT = re.compile(r"\D+")


def normalize_phone(raw: str | None) -> str:
    """Return the digits of `raw` (a phone), or "" for empty/None.

    ``+91 98000-00001`` -> ``919800000001``. Applied identically to the
    inbound handle and the roster cell so formatting never causes a miss.
    """
    if not raw:
        return ""
    return _NON_DIGIT.sub("", str(raw))
