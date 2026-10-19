"""The approval gate around every tool the agent can call.

``apply`` wraps a FunctionTool registry (hub tools, local tools, restricted
tools and, on Codex, MCP tools), so the OpenAI Agents, Claude and Codex
runtimes and hosted MCP all reach the same check before a tool runs. Claude
also calls external MCP servers itself; ``claude_hook`` is the PreToolUse
hook that applies the same check to those calls.

The gate runs after access control: a person who may not use a tool is
refused by the access guard first. A tool whose rule asks returns a short
"waiting for approval" result instead of running; the agent tells the person
and ends its turn (see ``service``).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .. import _request_ctx
from . import service

log = logging.getLogger("hubzoid.hitl")

#: tools that are part of asking itself, or never consequential
EXEMPT = frozenset({"ask_question", "request_status"})

WAITING = ("Waiting for {who}'s approval: \"{title}\" has not run. Tell them in one short sentence what you "
           "want to do and why, say you are waiting for their approval, and end your turn now. Do not call "
           "any more tools in this turn.")


def hub_id_of(hub_dir: Path) -> str:
    return Path(hub_dir).name.lower()


def caller() -> service.Caller | None:
    """Who the current tool call acts for, from the run's context."""
    slot = _request_ctx.native_session()
    info = getattr(slot, "hitl", None) if slot is not None else None
    if info:
        return service.Caller(**info)
    from ..access.identity import current_identity

    ident = current_identity()
    if ident.user is None:
        return None
    surface = ident.surface or "system"
    if surface in ("workflow", "system"):
        return service.Caller(account_id=ident.user, email=ident.user, present=False, origin="workflow")
    origin = {"mcp": "mcp", "slack": "slack", "whatsapp": "whatsapp"}.get(surface.split("-")[0], "api")
    return service.Caller(account_id=ident.user, email=ident.user, present=True, origin=origin,
                          conversation_id=_request_ctx.get_chat_id())


def _arguments(input_str) -> object:
    if isinstance(input_str, (dict, list)):
        return input_str
    text = (input_str or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {"input": text}


def note_waiting(tool: str, request: dict) -> None:
    """Tell the live run a tool is waiting, so its card shows in the chat."""
    slot = _request_ctx.native_session()
    if slot is None:
        return
    pending = getattr(slot, "hitl_waiting", None)
    if pending is None:
        pending = []
        slot.hitl_waiting = pending
    pending.append({"tool": tool, "request_id": request["id"], "kind": request["kind"]})


def suspended() -> bool:
    slot = _request_ctx.native_session()
    notes = getattr(slot, "hitl_waiting", None) if slot is not None else None
    return any(not n.get("card") for n in notes or ())  # a card is not a wait


def evaluate(hub_dir: Path, tool: str, arguments, *, connector: str | None = None,
             read_only: bool | None = None) -> service.Verdict:
    who = caller()
    if who is None:
        return service.Verdict("allow")  # an anonymous call is refused by access control, not here
    if suspended():
        return service.Verdict("refuse", message="This turn is waiting for the person's answer. Stop here.")
    return service.check(hub_dir, hub_id_of(hub_dir), who, tool=tool, arguments=arguments, connector=connector,
                         read_only=read_only, auto_check=_auto_check(hub_dir))


def waiting_text(verdict: service.Verdict) -> str:
    req = verdict.request or {}
    who = "the person" if req.get("requester_email") is None else "the requester"
    out = {"status": "waiting", "request_id": req.get("id"), "kind": req.get("kind"),
           "message": WAITING.format(who=who, title=req.get("title", "this"))}
    return json.dumps(out)


def wrap(ft, hub_dir: Path, *, connector: str | None = None, read_only: bool | None = None):
    """A copy of FunctionTool ``ft`` that asks first when its rule says so."""
    if ft.name in EXEMPT or getattr(ft, "hitl_wrapped", False):
        return ft
    original = ft.on_invoke_tool
    hub_dir = Path(hub_dir)

    async def invoke(ctx, input_str):
        args = _arguments(input_str)
        verdict = await asyncio.to_thread(evaluate, hub_dir, ft.name, args, connector=connector,
                                          read_only=read_only)
        if verdict.action == "allow":
            return await original(ctx, input_str)
        if verdict.action == "refuse":
            return verdict.message
        note_waiting(ft.name, verdict.request)
        return waiting_text(verdict)

    from ..tool_classes import copy_tool

    out = copy_tool(ft, on_invoke_tool=invoke)
    try:
        out.hitl_wrapped = True
    except Exception:  # noqa: BLE001 - frozen copies just lose the marker
        pass
    return out


def guard_mcp_server(server, hub_dir: Path):
    """Gate every call an MCP server object receives (the OpenAI Agents SDK
    calls servers directly, not through FunctionTools). Same rules as ``wrap``,
    with the server's readOnlyHint for ``ask: writes``."""
    if getattr(server, "hitl_guarded", False):
        return server
    from mcp.types import CallToolResult, TextContent

    original = server.call_tool
    hub_dir = Path(hub_dir)
    connector = getattr(server, "name", None)
    hints: dict = {}

    async def call_tool(tool_name, arguments=None, *args, **kwargs):
        if not hints.get("_listed"):
            try:
                for tool in await server.list_tools() or []:
                    a = getattr(tool, "annotations", None)
                    if a is not None and getattr(a, "readOnlyHint", None) is not None:
                        hints[tool.name] = bool(a.readOnlyHint)
            except Exception:  # noqa: BLE001 - no hints: every tool counts as a write
                pass
            hints["_listed"] = True
        verdict = await asyncio.to_thread(evaluate, hub_dir, tool_name, arguments or {}, connector=connector,
                                          read_only=hints.get(tool_name))
        if verdict.action == "allow":
            return await original(tool_name, arguments, *args, **kwargs)
        if verdict.action == "refuse":
            text = verdict.message
        else:
            note_waiting(tool_name, verdict.request)
            text = waiting_text(verdict)
        return CallToolResult(content=[TextContent(type="text", text=text)], isError=False)

    server.call_tool = call_tool
    server.hitl_guarded = True
    return server


def apply(hub_dir: Path, registry: dict, *, connector: str | None = None) -> dict:
    return {name: wrap(ft, hub_dir, connector=connector) for name, ft in registry.items()}


def claude_hook(hub_dir: Path, hub_namespace: str):
    """A PreToolUse hook for MCP tools Claude calls directly (``mcp__<server>__<tool>``).
    Hub tools (the ``hub_namespace`` server) are already wrapped."""
    hub_dir = Path(hub_dir)

    async def hook(tool_input, _tool_use_id, _context):
        name = (tool_input or {}).get("tool_name") or ""
        if not name.startswith("mcp__") or name.startswith(f"mcp__{hub_namespace}__"):
            return {}
        parts = name.split("__", 2)
        connector = parts[1] if len(parts) == 3 else None
        args = (tool_input or {}).get("tool_input") or {}
        verdict = await asyncio.to_thread(evaluate, hub_dir, name, args, connector=connector)
        if verdict.action == "allow":
            return {}
        if verdict.action == "ask":
            note_waiting(name, verdict.request)
            reason = waiting_text(verdict)
        else:
            reason = verdict.message
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}

    return hook


AUTO_INSTRUCTIONS = (
    "Review the exact tool action for dangerous effects. Return caution when it could cause meaningful harm, "
    "including destructive data loss, credential exposure, financial transactions, deployments or public "
    "changes, external communication, privilege or system changes, or concealed execution. Return clear for "
    "routine, low-impact actions. If important effects are unclear, return caution.")


def _auto_check(hub_dir: Path):
    """``auto`` (eve's ``auto()`` policy, the same Jev decision model): a clear
    routine call runs, anything else asks a person. Without Jev's key, or on
    any failure, it asks a person."""
    def check(tool: str, arguments) -> bool:
        from .. import jev

        try:
            reply = jev.call(Path(hub_dir), {
                "state": {"action": {"tool": tool, "input": arguments}},
                "questions": {"permission": {"type": "choice", "instructions": AUTO_INSTRUCTIONS,
                                             "criteria": {"clear": "The action is routine and low impact.",
                                                          "caution": "The action is dangerous or unclear."}}},
                "timeout": 30}, surface="hitl")
        except Exception:  # noqa: BLE001 - no key or no answer: a person decides
            return False
        answer = (reply.get("answers") or {}).get("permission") or {}
        return answer.get("choice") == "clear"

    return check
