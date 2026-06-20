"""HubZoid - markdown-driven AI agent platform.

Drop AGENTS.md + skills/ + knowledge/ into a folder, get a working chat agent
with a polished web UI. See README.md.
"""
from __future__ import annotations

__version__ = "0.6.7"

from .factory import build_agent  # noqa: E402,F401  (public re-export)
