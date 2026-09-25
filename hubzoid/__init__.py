"""HubZoid - markdown-driven AI agent platform.

Drop AGENTS.md + skills/ + knowledge/ into a folder, get a working chat agent
with a polished web UI. See README.md.
"""
from __future__ import annotations

try:  # one source of truth: pyproject.toml, via the installed package metadata
    from importlib.metadata import version as _version

    __version__ = _version("hubzoid")
except Exception:  # noqa: BLE001 — running from a source tree that is not installed
    __version__ = "0+unknown"

from .factory import build_agent  # noqa: E402,F401  (public re-export)


def __getattr__(name):
    # Lazy re-export of the workflow façade so `import hubzoid` never pulls in
    # DBOS. Authors write `from hubzoid import workflow, step, hub`.
    if name in ("workflow", "step", "hub"):
        from . import workflows

        return getattr(workflows, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
