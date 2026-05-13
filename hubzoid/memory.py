"""Session bookkeeping — per-process session id used for memory + artifacts.

v1: one session id per `hubzoid run` invocation, shared across all chat turns.
Multi-tenant sessions (per-customer or per-conversation) come in v1.1.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path


def make_session_id() -> str:
    """Return a short, sortable session id (timestamp-style)."""
    return os.environ.get("HUBZOID_SESSION_ID") or uuid.uuid4().hex[:12]


def session_output_dir(hub_dir: Path, session_id: str) -> Path:
    p = hub_dir / "output" / session_id
    p.mkdir(parents=True, exist_ok=True)
    return p
