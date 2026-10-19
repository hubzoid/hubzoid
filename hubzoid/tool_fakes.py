"""Fake tool results for evals (GitHub discussion #62).

A case that declares ``fake_tools`` runs with its hub's own tools answered by
those fakes instead of running for real:

    fake_tools:
      - tool: lookup_order
        args: {order_id: A-1001}          # optional: only calls with these arguments
        output: {status: cancelled}       # text, or anything JSON
      - tool: send_email
        error: SMTP is down               # the call fails with this message
    real_tools: [read_knowledge]          # may still run for real in this case

Inside such a case every other call of a hub tool is blocked: the agent gets
"Blocked in this eval" instead of a real effect, and the case fails, so a
missing fake can never send an email or write to a system. Calls are matched
after the access check (``access.guard`` wraps the fake inside its check), so
a person who may not use a tool is still refused.

Outside a case nothing changes: the wrapper reads one ContextVar and calls
the tool. MCP servers are not covered (their calls never pass through here),
so the runner refuses a faked case on a hub that has MCP servers, and the
person's own connectors are left out of the case (``withheld``), unless the
case says ``allow_real_mcp: true``.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

BLOCKED = "Blocked in this eval: no fake result for {name} with these arguments. Nothing was run."
_MARK = "__hubzoid_fakes__"


@dataclass(frozen=True)
class Fake:
    tool: str
    args: dict | None = None
    output: Any = None
    error: str | None = None

    def matches(self, name: str, args: Any) -> bool:
        if name != self.tool:
            return False
        if not self.args:
            return True
        from .evals.calls import args_match  # loaded only while a faked case runs

        return args_match(args, self.args)

    def result(self) -> str:
        if self.error is not None:
            return f"Error: {self.error}"
        return self.output if isinstance(self.output, str) else json.dumps(self.output)


@dataclass
class Session:
    """One case's fakes, and what happened. Mutated in place, so the record
    survives the context copies runtimes make for tool calls."""
    fakes: list[Fake]
    real: frozenset[str] = frozenset()
    allow_mcp: bool = False
    used: list[str] = field(default_factory=list)
    blocked: list[tuple[str, Any]] = field(default_factory=list)
    left_out: list[str] = field(default_factory=list)   # the person's connectors, not offered


_active: ContextVar[Session | None] = ContextVar("hubzoid_eval_fakes", default=None)


def withheld(connector_ids) -> bool:
    """True while a faked case runs that may not use real MCP servers: the
    person's connectors (``connector_ids``) are then not offered, and noted."""
    session = _active.get()
    if session is None or session.allow_mcp:
        return False
    session.left_out.extend(c for c in connector_ids if c not in session.left_out)
    return True


@contextmanager
def scope(session: Session | None) -> Iterator[Session | None]:
    token = _active.set(session)
    try:
        yield session
    finally:
        _active.reset(token)


def _parse_args(raw: str) -> Any:
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return raw


def wrap(ft):
    """``ft`` with fakes consulted on every call while a faked case runs."""
    if getattr(ft.on_invoke_tool, _MARK, False):
        return ft
    original = ft.on_invoke_tool
    name = ft.name

    async def _invoke(ctx, input_str):
        session = _active.get()
        if session is None or name in session.real:
            return await original(ctx, input_str)
        args = _parse_args(input_str)
        for fake in session.fakes:
            if fake.matches(name, args):
                session.used.append(name)
                return fake.result()
        session.blocked.append((name, args))
        return BLOCKED.format(name=name)

    setattr(_invoke, _MARK, True)
    from .tool_classes import copy_tool  # keeps the tool's class (read, external write) and markers

    return copy_tool(ft, on_invoke_tool=_invoke)


def covered(ft):
    """Mark a tool whose fakes are already consulted inside it (a guarded
    restricted tool), so ``wrap_registry`` does not put them in front of its
    access check."""
    try:
        setattr(ft.on_invoke_tool, _MARK, True)
    except (AttributeError, TypeError):
        pass
    return ft


def wrap_registry(registry: dict) -> dict:
    """Every tool of a runtime's registry, wrapped (the factories' last step)."""
    return {name: wrap(ft) for name, ft in registry.items()}


def wrap_added(before: dict, after: dict) -> dict:
    """``after`` with the tools that are not in ``before`` wrapped. A layer that
    wraps every tool again (the approval gate) keeps the fakes inside it, and
    a tool it adds (``ask_question``) gets them too."""
    return {name: (ft if name in before else wrap(ft)) for name, ft in after.items()}
