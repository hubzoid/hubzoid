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
missing fake can never send an email or write to a system. An error fake
answers as a tool that raised does, so every runtime and the eval report see
a failed call. Calls are matched after the access checks: ``access.guard``
and the workflow guard wrap the fake inside their check, and the mark that
says so is on the tool, so every copy (``tool_classes.copy_tool``) made by a
later wrapper keeps that wrapper in front of the fake. A person who may not
use a tool is still refused, and the refusal is audited.

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
#: What a tool that raised returns on the Agents SDK (its default
#: ``failure_error_function``); every runtime reads it as a failed call
#: (``tool_events.failed_output``). An error fake answers with exactly this.
TOOL_ERROR = "An error occurred while running the tool. Please try again. Error: {error}"
_MARK = "_hz_fakes"     # on a tool whose calls already consult the fakes


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
            return TOOL_ERROR.format(error=self.error)
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
    """``ft`` with fakes consulted on every call while a faked case runs. A
    tool that already consults them (itself, or inside a check: ``covered``)
    is returned as it is."""
    if getattr(ft, _MARK, False):
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

    from .tool_classes import copy_tool  # keeps the tool's class (read, external write) and markers

    return covered(copy_tool(ft, on_invoke_tool=_invoke))


def covered(ft):
    """Mark a tool whose fakes are already consulted inside it (a guarded
    restricted tool), so ``wrap_registry`` does not put them in front of its
    access check. The mark is on the tool, so a copy made by a later wrapper
    (``access.guard.run_checked``, cards, approvals) keeps it."""
    try:
        setattr(ft, _MARK, True)
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
