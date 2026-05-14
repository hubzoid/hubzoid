"""Read <hub>/connectors/.mcp.json and build MCP server objects.

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
    cdir = resolve_bucket(hub_dir, "connectors")
    if cdir is None:
        return {}
    mcp_file = cdir / ".mcp.json"
    if not mcp_file.is_file():
        mcp_file = cdir / "mcp.json"  # fallback without leading dot
    if not mcp_file.is_file():
        return {}

    try:
        cfg = json.loads(mcp_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{mcp_file}: invalid JSON — {exc}") from exc

    servers_cfg = cfg.get("mcpServers") or {}
    if not isinstance(servers_cfg, dict):
        raise ValueError(f"{mcp_file}: `mcpServers` must be a mapping.")

    return {
        name: _interpolate(spec)
        for name, spec in servers_cfg.items()
        if isinstance(spec, dict)
    }


def load_all(hub_dir: Path) -> list:
    """OpenAI Agents SDK wrapper around `load_all_raw`.

    Returns MCPServerSse / MCPServerStdio objects ready to attach to an Agent.
    """
    from agents.mcp import MCPServerSse, MCPServerStdio

    out: list = []
    for name, spec in load_all_raw(hub_dir).items():
        transport = (spec.get("transport") or "stdio").lower()
        try:
            if transport == "sse" or spec.get("url"):
                server = MCPServerSse(
                    params={"url": spec["url"], "headers": spec.get("headers", {})},
                    name=name,
                )
            else:
                server = MCPServerStdio(
                    params={
                        "command": spec["command"],
                        "args": spec.get("args", []),
                        "env": spec.get("env", {}),
                    },
                    name=name,
                )
            out.append(server)
        except KeyError as exc:
            log.warning("MCP server %r missing required field %s; skipping", name, exc)
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
