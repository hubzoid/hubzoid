"""Workflows that ship with Hubzoid (loaded beside a hub's own workflows)."""
from __future__ import annotations

from pathlib import Path

DIR = Path(__file__).resolve().parent
NAMES = ("repeated_request",)


def files() -> list[Path]:
    """The built-in workflow files, unless switched off with HUBZOID_PROMPT_WORKFLOWS=false."""
    import os

    if (os.environ.get("HUBZOID_PROMPT_WORKFLOWS") or "").strip().lower() in ("0", "false", "no", "off"):
        return []
    return [DIR / f"{n}.py" for n in NAMES]
