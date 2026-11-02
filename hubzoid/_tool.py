"""``hubzoid.tool``: the one way to make a tool, built-in or in a hub.

It is the OpenAI Agents ``function_tool`` with a non-strict schema. A strict
schema lists every argument as required (a ``None`` default becomes a required
nullable value), and that schema goes unchanged to claude-local, codex-local
(``dynamicTools``) and MCP clients, which then refuse a call that leaves an
optional argument out. A strict schema cannot be loosened later, because it no
longer says which arguments were optional, so the choice is made here, where
the tool is defined. Only arguments without a default are required.

    from hubzoid import tool

    @tool
    def lookup(order_id: str, limit: int = 10) -> str: ...

The tool carries ``strict_json_schema=False``, so the OpenAI runtime is told it
is non-strict too and never receives a relaxed schema marked strict. Any other
``function_tool`` keyword (``name_override``, ``description_override``, ...)
passes through.
"""
from __future__ import annotations

from typing import Any


def tool(func: Any = None, **kwargs: Any) -> Any:
    """``@tool`` or ``@tool(name_override=...)``: a non-strict ``FunctionTool``."""
    from agents import function_tool

    kwargs.setdefault("strict_mode", False)
    if func is None:
        return lambda f: function_tool(f, **kwargs)
    return function_tool(func, **kwargs)


def optional_marked_required(ft: Any) -> list[str]:
    """Arguments a strict tool marks required although they look optional (a
    default, or ``null`` allowed). Empty for a non-strict tool. Used by the hub
    tool loader and ``hubzoid doctor`` to name old-style hub tools."""
    if not getattr(ft, "strict_json_schema", False):
        return []
    schema = getattr(ft, "params_json_schema", None) or {}
    out = []
    for name, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        options = prop.get("anyOf") or [prop]
        nullable = any(isinstance(o, dict) and o.get("type") == "null" for o in options)
        if "default" in prop or nullable or (isinstance(prop.get("type"), list) and "null" in prop["type"]):
            out.append(name)
    return out
