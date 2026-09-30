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


def __getattr__(name):
    # Lazy public re-exports, so `import hubzoid` stays light: the CLI, the edge
    # and the web app start without loading the agent SDKs (OpenAI Agents,
    # LiteLLM) or DBOS until something uses them.
    # `from hubzoid import build_agent` and `from hubzoid import workflow, step,
    # hub` work as before.
    if name == "build_agent":
        from .factory import build_agent

        return build_agent
    if name in ("workflow", "step", "hub"):
        from . import workflows

        return getattr(workflows, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
