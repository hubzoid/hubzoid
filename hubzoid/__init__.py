"""HubZoid - markdown-driven AI agent platform.

Drop AGENTS.md + skills/ + knowledge/ into a folder, get a working chat agent
with a polished web UI. See README.md.
"""
from __future__ import annotations

__version__ = "0.9.5"

from .factory import build_agent  # noqa: E402,F401  (public re-export)


def __getattr__(name):
    # Lazy re-export of the workflow façade so `import hubzoid` never pulls in
    # DBOS. Authors write `from hubzoid import workflow, step, hub`.
    if name in ("workflow", "step", "hub"):
        from . import workflows

        return getattr(workflows, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
