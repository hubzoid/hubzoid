"""Which kind of Hubzoid process this is.

``HUBZOID_ROLE`` is ``runner`` (an AgentCore microVM running agent turns) or
``mcp`` (an AgentCore microVM serving the hub's MCP server). Unset, as in every
existing deployment, means an ordinary process and changes nothing.

A runner or MCP process never owns shared state: it does not migrate the
database, take migration locks, start the workflow engine or the scheduler,
sweep runs, or route channels. Those belong to the hub service.
"""
from __future__ import annotations

import os

ENV = "HUBZOID_ROLE"
RUNNER = "runner"
MCP = "mcp"


def current(env=None) -> str:
    return ((os.environ if env is None else env).get(ENV) or "").strip().lower()


def is_remote(env=None) -> bool:
    """True inside an AgentCore runner or MCP microVM."""
    return current(env) in (RUNNER, MCP)


class RoleError(RuntimeError):
    """Work that only the hub service may do was attempted in a runner."""


def refuse(what: str) -> None:
    """Refuse hub-service-only work in a runner or MCP process."""
    if is_remote():
        raise RoleError(f"{what} runs only in the hub service, not in an AgentCore {current()} process.")
