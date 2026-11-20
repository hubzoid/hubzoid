"""What a turn's process context holds, carried from the hub service to a runner.

Inside one Hubzoid process a turn reads its caller, chat, images, deadline and
usage attribution from context variables (``hubzoid._request_ctx``,
``hubzoid.access``, ``hubzoid.usage``, ``hubzoid.workflows.deadlines``,
``hubzoid.embed.scope``). A runner is another process, so the hub service
captures them per call (``capture``) and the runner enters the same scopes
(``restore``). The caller's identity travels in the work order, not the request.
"""
from __future__ import annotations

import contextlib
from typing import Any, Iterator


def identity_dict(ident) -> dict:
    app = getattr(ident, "app", None)
    return {
        "user": ident.user, "groups": sorted(ident.groups), "surface": ident.surface,
        "account_id": ident.account_id, "auth_epoch": ident.auth_epoch,
        "run_identity_id": ident.run_identity_id,
        "hubs": list(ident.hubs) if ident.hubs is not None else None,
        "credential": list(ident.credential) if ident.credential else None,
        "app": ({"app_id": app.app_id, "agents": sorted(app.agents), "tools": sorted(app.tools)}
                if app is not None else None),
    }


def identity_from(d: dict | None):
    from ..access import identity as idlib

    if not d or (not d.get("user") and not d.get("app")):
        return idlib.ANONYMOUS
    app = d.get("app")
    return idlib.Identity.make(
        d.get("user"), d.get("groups") or (), surface=d.get("surface") or "system",
        account_id=d.get("account_id"), auth_epoch=d.get("auth_epoch"),
        run_identity_id=d.get("run_identity_id"),
        hubs=tuple(d["hubs"]) if d.get("hubs") is not None else None,
        credential=tuple(d["credential"]) if d.get("credential") else None,
        app=idlib.AppScope(app["app_id"], frozenset(app.get("agents") or ()), frozenset(app.get("tools") or ()))
        if app else None)


def capture() -> dict[str, Any]:
    """The hub service's side: everything the turn's context holds, JSON-safe."""
    from .. import _request_ctx, usage
    from ..access import current_identity
    from ..embed import scope as app_scope
    from ..workflows import deadlines

    ident = current_identity()
    slot = _request_ctx.native_session()
    live = app_scope._live.get()
    read_scope = _request_ctx.get_schedule_read_scope()
    out: dict[str, Any] = {
        "chat": _request_ctx.get_chat_id(),
        "images": list(_request_ctx._current_images.get()),
        "client": _request_ctx.get_client(),
        "run_tag": usage._RUN_TAG.get(),
        "trigger": usage._TRIGGER.get(),
        "deadline": deadlines._deadline.get(),
        "zone": _request_ctx.browser_zone(ident.account_id),
        "schedule_read": [str(read_scope[0]), str(read_scope[1])] if read_scope else None,
        "app_token": ({"app_id": live.app_id, "token": live.token, "token_to": list(live.token_to),
                       "header": live.header} if live is not None else None),
    }
    if slot is not None:
        out["slot"] = {
            "resume_id": slot.resume_id, "delta_prompt": slot.delta_prompt,
            "resume_cost": slot.resume_cost, "images": slot.images, "delta_images": slot.delta_images,
            "web_search": bool(slot.web_search), "model": slot.model,
            "tool_allowlist": slot.tool_allowlist, "hitl": getattr(slot, "hitl", None),
            "new_input": getattr(slot, "new_input", None),
        }
    return out


@contextlib.contextmanager
def restore(ctx: dict[str, Any], identity: dict | None, guard) -> Iterator[Any]:
    """The runner's side: enter the turn's scopes. Yields the session slot
    (or None) so the runner can send its outputs back."""
    from pathlib import Path

    from .. import _request_ctx, usage
    from ..access import identity_scope
    from ..embed import scope as app_scope
    from ..workflows import deadlines

    ident = identity_from(identity)
    if ident.account_id and ctx.get("zone"):
        _request_ctx.remember_browser_zone(ident.account_id, ctx["zone"])
    slot = None
    s = ctx.get("slot")
    if s is not None:
        slot = _request_ctx.SessionSlot(resume_id=s.get("resume_id"), delta_prompt=s.get("delta_prompt"),
                                        images=s.get("images"), delta_images=s.get("delta_images"),
                                        resume_cost=s.get("resume_cost"))
        slot.web_search = bool(s.get("web_search"))
        slot.model = s.get("model")
        slot.tool_allowlist = s.get("tool_allowlist")
        if s.get("hitl") is not None:
            slot.hitl = s["hitl"]
        if s.get("new_input") is not None:
            slot.new_input = s["new_input"]
        slot.guard = guard
    with contextlib.ExitStack() as stack:
        stack.enter_context(_request_ctx.chat_scope(ctx.get("chat")))
        stack.enter_context(identity_scope(ident))
        stack.enter_context(_request_ctx.image_scope(ctx.get("images") or ()))
        stack.enter_context(_request_ctx.native_session_scope(slot))
        stack.enter_context(_request_ctx.call_guard_scope(guard))
        if ctx.get("client"):
            _request_ctx.set_client(ctx["client"])
        if ctx.get("schedule_read"):
            stack.enter_context(_request_ctx.schedule_read_scope(Path(ctx["schedule_read"][0]),
                                                                 ctx["schedule_read"][1]))
        if ctx.get("run_tag") is not None:
            tok = usage._RUN_TAG.set(ctx["run_tag"])
            stack.callback(usage._RUN_TAG.reset, tok)
        if ctx.get("trigger") is not None:
            tok2 = usage._TRIGGER.set(ctx["trigger"])
            stack.callback(usage._TRIGGER.reset, tok2)
        if ctx.get("deadline") is not None:
            stack.enter_context(deadlines.scope(absolute=float(ctx["deadline"]), check_exit=False))
        live = ctx.get("app_token")
        if live:
            tok3 = app_scope._live.set(app_scope._Live(live["app_id"], live.get("token"),
                                                       tuple(live.get("token_to") or ()), live.get("header") or ""))
            stack.callback(app_scope._live.reset, tok3)
        yield slot


def outputs(slot) -> dict[str, Any]:
    """The runner's side, at the end: what the hub service's context must hold
    afterwards, as the turn would have left it in-process."""
    from .. import _request_ctx

    out: dict[str, Any] = {"usage": _request_ctx.drain_usage(), "failure": _request_ctx.run_failure(),
                           "artifacts": [dict(a) for a in (_request_ctx._current_artifacts.get() or [])
                                         if isinstance(a, dict)]}
    if slot is not None:
        out["slot"] = {"session_id": slot.session_id, "session_cost": slot.session_cost,
                       "resumed": slot.resumed, "runtime": slot.runtime}
    return out


def apply(out: dict[str, Any]) -> None:
    """The hub service's side: put the runner's outputs into this context."""
    from .. import _request_ctx

    if out.get("usage"):
        _request_ctx.record_usage(out["usage"])
    failure = out.get("failure") or {}
    if failure.get("kind"):
        _request_ctx.note_run_failure(failure["kind"], reset_at=failure.get("reset_at"))
    for a in out.get("artifacts") or []:
        if isinstance(a, dict) and a.get("name") and a.get("url"):
            _request_ctx.record_artifact(a["name"], a["url"])
    slot = _request_ctx.native_session()
    s = out.get("slot")
    if slot is not None and s:
        slot.session_id = s.get("session_id")
        slot.session_cost = s.get("session_cost")
        slot.resumed = bool(s.get("resumed"))
        slot.runtime = s.get("runtime")
