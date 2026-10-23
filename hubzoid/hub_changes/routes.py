"""HTTP routes for hub changes and personal memory, on each hub's bridge.

  GET  /api/hub-changes?status=pending|applied|attention   proposals, newest first
  GET  /api/hub-changes/{id}                               one, with its diff
  POST /api/hub-changes                                    propose {title, reason, edits}
  POST /api/hub-changes/{id}/decision                      {decision: approve|reject, expected_revision, note}
  POST /api/hub-changes/{id}/undo                          propose putting the files back

  GET    /api/memory          my notes on this hub
  PUT    /api/memory          replace them {text, expected_revision}
  DELETE /api/memory          delete them

Seeing proposals needs Save shared knowledge or hub management; deciding
needs hub management and a person's own session (never an API key).
"""
from __future__ import annotations

from fastapi import FastAPI, Request

from .. import editing, personal_memory
from ..chat.common import db
from ..chat.engine_routes import read_json
from ..control.http import error
from ..hitl.routes import human_only
from . import service


def _err(exc):
    return error(exc.status, exc.code, exc.message, **exc.extra)


def mount(app: FastAPI, ctx) -> None:
    hub_dir = ctx.hub_dir

    async def who(request: Request, *, mutation: bool = False):
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        return user.id, (getattr(user, "email", "") or "").lower()

    async def reader(request: Request, *, mutation: bool = False) -> tuple[str, bool]:
        _uid, email = await who(request, mutation=mutation)
        review = await db(service.can_review, hub_dir, email)
        if not review and not await db(service.can_propose, hub_dir, email):
            raise error(403, "permission_denied", "Only people who may change this hub's knowledge see its "
                                                  "proposed changes.")
        return email, review

    async def applied(ch: dict) -> dict:
        """The change as the API shows it. Once instructions or skills changed,
        the chat's runtime is rebuilt so the next reply uses them (knowledge
        is read live)."""
        out = service.public(ch)
        if ch["status"] == "applied" and service.needs_reload(ch):
            from ..chat.authoring_routes import _reload_runtime

            out["runtime_reloaded"] = await _reload_runtime(ctx)
        return out

    @app.get("/api/hub-changes")
    async def list_changes(request: Request, status: str | None = None):
        email, review = await reader(request)
        items = await db(service.list_, hub_dir, status=status or None)
        return {"items": [service.public(c, with_diff=False) for c in items], "can_review": review}

    @app.get("/api/hub-changes/{cid}")
    async def get_change(cid: int, request: Request):
        email, review = await reader(request)
        ch = await db(service.get, hub_dir, cid)
        if ch is None:
            raise error(404, "not_found", "There is no such change.")
        return {**service.public(ch), "can_review": review}

    @app.post("/api/hub-changes")
    async def propose(request: Request):
        _uid, email = await who(request, mutation=True)
        editing.check(hub_dir, "the agent's instructions and knowledge")
        body = await read_json(request)
        try:
            ch = await db(service.propose, hub_dir, title=body.get("title"), edits=body.get("edits"),
                          reason=str(body.get("reason") or ""), actor=email, via="api",
                          present=not (request.headers.get("authorization") or "").lower().startswith("bearer "))
        except service.ChangeError as exc:
            raise _err(exc)
        return await applied(ch)

    @app.post("/api/hub-changes/{cid}/decision")
    async def decide(cid: int, request: Request):
        user = await ctx.mutation(request)
        human_only(request, user)
        email = (getattr(user, "email", "") or "").lower()
        body = await read_json(request)
        if str(body.get("decision") or "") != "reject":
            # Rejecting a waiting change is always allowed; applying writes files.
            editing.check(hub_dir, "the agent's instructions and knowledge")
        try:
            ch = await db(service.decide, hub_dir, cid, actor=email, decision=str(body.get("decision") or ""),
                          expected_revision=body.get("expected_revision"), note=body.get("note"))
        except service.ChangeError as exc:
            raise _err(exc)
        return await applied(ch)

    @app.post("/api/hub-changes/{cid}/undo")
    async def undo(cid: int, request: Request):
        _uid, email = await who(request, mutation=True)
        editing.check(hub_dir, "the agent's instructions and knowledge")
        await reader(request)
        try:
            ch = await db(service.undo, hub_dir, cid, actor=email)
        except service.ChangeError as exc:
            raise _err(exc)
        return service.public(ch)

    @app.get("/api/editing")
    async def editing_status(request: Request):
        """Whether the web app offers editors for this agent's files (Beta)."""
        await reader(request)
        return editing.status(hub_dir)

    # -- personal memory ----------------------------------------------------------------
    @app.get("/api/memory")
    async def get_memory(request: Request):
        uid, _email = await who(request)
        return await db(personal_memory.get, hub_dir, uid)

    @app.put("/api/memory")
    async def put_memory(request: Request):
        uid, email = await who(request, mutation=True)
        body = await read_json(request)
        try:
            return await db(personal_memory.put, hub_dir, uid, email, str(body.get("text") or ""),
                            expected_revision=body.get("expected_revision"))
        except personal_memory.MemoryError_ as exc:
            raise _err(exc)

    @app.delete("/api/memory")
    async def delete_memory(request: Request):
        uid, email = await who(request, mutation=True)
        return await db(personal_memory.put, hub_dir, uid, email, "")
