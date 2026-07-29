"""At-least-once redelivery guard for webhook surfaces.

Meta and Telegram both redeliver a message if they don't get a fast 200 (or on
their own retry schedule). We must act on each id once. The claim is an atomic
exclusive file create keyed on a hash of the message id: the first delivery
creates the marker and returns "new"; any redelivery finds it and returns
"duplicate". Persisted on disk so a restart mid-stream still drops the repeat.

Per-key files, no database — one small marker per message. (A periodic prune of
old markers can be added later; markers are a few dozen bytes each.)
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


class Dedup:
    """Claim message ids exactly once, backed by marker files in `directory`."""

    def __init__(self, directory) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def claim(self, message_id: str) -> bool:
        """Return True if `message_id` is new (claimed now), False if already seen.

        Atomic: the exclusive create is the dedup — two concurrent deliveries of
        the same id can never both get True.
        """
        marker = self.dir / _marker_name(message_id)
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        os.close(fd)
        return True


def _marker_name(message_id: str) -> str:
    """A fixed-length, path-safe filename for any id (hashed, so `/` etc. are safe)."""
    return hashlib.sha256((message_id or "").encode("utf-8")).hexdigest()
