"""Read <hub>/connectors/.mcp.json into runtime-neutral server configuration.

Supported shapes (subset of the MCP / Claude Desktop config format):

  {
    "mcpServers": {
      "filesystem": {
        "command": "npx",
        "args": ["@modelcontextprotocol/server-filesystem", "./workspace"],
        "env": {"FOO": "bar"}
      },
      "remote": {
        "transport": "sse",
        "url": "https://example.com/mcp/sse"
      }
    }
  }

Env-var interpolation: any ${NAME} inside string fields is replaced with
the value of NAME from the current environment (after .env is loaded).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from .._fs import resolve_bucket

log = logging.getLogger(__name__)
_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def load_all_raw(hub_dir: Path) -> dict[str, dict]:
    """Return runtime-neutral MCP server configs: `{name: spec}`.

    Each spec is the parsed, env-interpolated dict (command/args/env, or
    transport/url/headers). Runtime adapters wrap these into engine-specific
    objects. Empty dict if no config file is present.
    """
    servers_cfg: dict = {}
    cdir = resolve_bucket(hub_dir, "connectors")
    if cdir is not None:
        mcp_file = cdir / ".mcp.json"
        if not mcp_file.is_file():
            mcp_file = cdir / "mcp.json"  # fallback without leading dot
        if mcp_file.is_file():
            try:
                cfg = json.loads(mcp_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{mcp_file}: invalid JSON — {exc}") from exc
            servers_cfg = cfg.get("mcpServers") or {}
            if not isinstance(servers_cfg, dict):
                raise ValueError(f"{mcp_file}: `mcpServers` must be a mapping.")

    out = {
        name: _interpolate(spec)
        for name, spec in servers_cfg.items()
        if isinstance(spec, dict)
    }

    # Auto-inject the shared browser (HUBZOID_BROWSER=true) as a `playwright`
    # server, so every agent gets the tools with no per-hub config. A hub that
    # defines its own `playwright` entry in .mcp.json wins (manual override).
    from .. import browser as browserlib

    browser_entry = browserlib.mcp_config_entry_from_env()
    if browser_entry:
        for name, spec in browser_entry.items():
            out.setdefault(name, spec)

    return out


def _norm_transport(spec: dict) -> str:
    """Canonical transport for a neutral spec: sse | streamable-http | stdio."""
    t = (spec.get("transport") or "").lower()
    if t in ("http", "streamable_http", "streamablehttp", "streamable-http"):
        return "streamable-http"
    if t == "sse":
        return "sse"
    if t:
        return t
    # Backward-compatible default: bare url -> SSE (the browser injection sets
    # its transport explicitly, so it is unaffected).
    return "sse" if spec.get("url") else "stdio"


def load_all_claude(hub_dir: Path) -> dict[str, dict]:
    """Claude-Agent-SDK-shaped MCP configs: `{name: {type, ...}}`.

    The Claude SDK's ``McpServerConfig`` uses ``type`` (``http`` | ``sse`` |
    ``stdio``) — not the neutral ``transport`` key — and rejects unknown keys.
    This translates the neutral specs from :func:`load_all_raw` (including the
    auto-injected browser server) into that shape, dropping OpenAI-only hints
    like ``client_session_timeout_seconds``.
    """
    out: dict[str, dict] = {}
    for name, spec in load_all_raw(hub_dir).items():
        transport = _norm_transport(spec)
        if transport in ("sse", "streamable-http"):
            cfg: dict = {"type": "http" if transport == "streamable-http" else "sse",
                         "url": spec["url"]}
            if spec.get("headers"):
                cfg["headers"] = spec["headers"]
        else:
            cfg = {"type": "stdio", "command": spec["command"]}
            if spec.get("args"):
                cfg["args"] = spec["args"]
            if spec.get("env"):
                cfg["env"] = spec["env"]
        out[name] = cfg
    return out


def _interpolate(obj):
    """Recursively replace ${VAR} occurrences in any string with os.environ values."""
    if isinstance(obj, str):
        return _VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, list):
        return [_interpolate(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _interpolate(v) for k, v in obj.items()}
    return obj
