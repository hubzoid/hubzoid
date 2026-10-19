"""HTTP routes for approvals and questions, on each hub's bridge.

  GET  /api/requests                   what waits for me (or what I asked)
  GET  /api/requests/{id}              one request, with what I may do
  POST /api/requests/{id}/decisions    decide: a signed-in person only
  POST /api/requests/{id}/cancel       the requester withdraws it
  GET  /api/requests/events?after=N    request events, for live views
  GET  /api/hitl/config                hitl.yaml, its revision and any error
  POST /api/hitl/config/validate       check a document, change nothing
  PUT  /api/hitl/config                write it (hitl.manage), compare-and-set

Decisions come only from a person in a browser session from this site, or
the local owner. An API key, an MCP token or a model never decides: those
get ``human_action_required`` with the link to the request page.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request

from ..chat.common import db
from ..chat.engine_routes import read_json
from ..control.http import error
from . import config as cfglib
from . import service

log = logging.getLogger("hubzoid.hitl")


def _err(exc: service.RequestError):
    return error(exc.status, exc.code, exc.message, **exc.extra)


def human_only(request: Request) -> None:
    """Only a person's own session decides. A bearer credential (an API key or
    an MCP token) is automation, whatever it is allowed to read."""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        raise error(403, "human_action_required",
                    "A person must decide this in Hubzoid. Open the request page and decide there.")


def mount(app: FastAPI, ctx) -> None:
    hub_dir, hub = ctx.hub_dir, ctx.hub

    async def who(request: Request, *, mutation: bool = False):
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        return user.id, (getattr(user, "email", "") or "").lower()

    @app.get("/api/requests")
    async def list_requests(request: Request):
        uid, email = await who(request)
        status = request.query_params.get("status", "pending")
        status = None if status == "all" else status
        items = await db(service.list_for, hub_dir, hub, uid, email, status=status,
                         conversation_id=request.query_params.get("conversation_id"))
        waiting = [r for r in items if r.get("allowed_actions")]
        return {"items": items, "pending_count": len(waiting)}

    @app.get("/api/requests/events")
    async def request_events(request: Request):
        uid, email = await who(request)
        try:
            after = int(request.query_params.get("after") or 0)
        except ValueError:
            raise error(400, "invalid_request", "after is an event sequence number.")
        return {"items": await db(service.events_after, hub_dir, hub, after, account_id=uid, email=email)}

    @app.get("/api/requests/{rid}")
    async def get_request(rid: str, request: Request):
        uid, email = await who(request)
        try:
            return await db(service.get_for, hub_dir, hub, rid, uid, email)
        except service.RequestError as exc:
            raise _err(exc)

    @app.post("/api/requests/{rid}/decisions")
    async def decide(rid: str, request: Request):
        human_only(request)
        uid, email = await who(request, mutation=True)
        body = await read_json(request)
        try:
            out = await db(service.decide, hub_dir, hub, rid, account_id=uid, email=email,
                           decision=str(body.get("decision") or ""), expected_revision=body.get("expected_revision"),
                           digest=body.get("digest"), answer=body.get("answer"), note=body.get("note"),
                           via="web")
        except service.RequestError as exc:
            raise _err(exc)
        await _continue_chat(ctx, out)
        return out

    @app.post("/api/requests/{rid}/cancel")
    async def cancel(rid: str, request: Request):
        uid, email = await who(request, mutation=True)
        body = await read_json(request)
        try:
            out = await db(service.cancel, hub_dir, hub, rid, account_id=uid, email=email,
                           reason=str(body.get("note") or "Cancelled by the person who asked."))
            return await db(service.get_for, hub_dir, hub, rid, uid, email) if out is None else \
                service.with_actions(out, uid, email)
        except service.RequestError as exc:
            raise _err(exc)

    # -- settings ---------------------------------------------------------------
    async def manager(request: Request, *, mutation: bool = False):
        from ..control import authz

        p = await authz.require_request(request, hub_dir, "hitl.manage", hub, mutation=mutation)
        return p

    @app.get("/api/hitl/config")
    async def get_config(request: Request):
        await manager(request)
        text = await db(cfglib.current_text, hub_dir)
        broken = await db(cfglib.broken, hub_dir)
        return {"revision": cfglib.revision_of(text), "text": text, "exists": text is not None,
                "error": broken.as_dict() if broken else None}

    @app.post("/api/hitl/config/validate")
    async def validate_config(request: Request):
        await manager(request)
        body = await read_json(request)
        try:
            cfg = cfglib.parse(str(body.get("text") or ""))
        except cfglib.ConfigError as exc:
            return {"valid": False, "error": exc.as_dict()}
        return {"valid": True, "summary": summary(cfg)}

    @app.put("/api/hitl/config")
    async def put_config(request: Request):
        await manager(request, mutation=True)
        body = await read_json(request)
        try:
            cfg = await db(cfglib.apply, hub_dir, str(body.get("text") or ""),
                           expected_revision=str(body.get("expected_revision") or ""))
        except cfglib.ConfigError as exc:
            if exc.path == "revision":
                raise error(409, "revision_conflict", exc.message,
                            current_revision=cfglib.revision_of(await db(cfglib.current_text, hub_dir)))
            raise error(422, "invalid_config", exc.message, path=exc.path, line=exc.line)
        return {"revision": cfg.digest, "summary": summary(cfg)}


def summary(cfg: cfglib.Config) -> dict:
    return {"questions": cfg.questions, "approvers": list(cfg.approvers),
            "tools": {k: {"ask": v.ask, "allow_always": v.allow_always} for k, v in cfg.tools.items()},
            "connectors": {k: {"ask": v.ask, "allow_always": v.allow_always} for k, v in cfg.connectors.items()},
            "agents": cfg.agents}


async def _continue_chat(ctx, request: dict) -> None:
    orch = getattr(ctx, "orchestrator", None)
    if orch is None or request.get("origin") != "chat":
        return
    try:
        await orch.continue_request(request)
    except Exception:  # noqa: BLE001 - the decision is saved; the chat continues on the next view
        log.exception("hitl: could not continue the chat after %s", request.get("id"))


RECOVERY_SECONDS = 5.0


async def recovery_loop(ctx) -> None:
    """Continue chats whose request was decided elsewhere (the CLI, a
    channel, another bridge) or whose continuation a restart interrupted."""
    import asyncio

    import sqlalchemy as sa

    from . import store

    r = store.requests.c
    while True:
        await asyncio.sleep(RECOVERY_SECONDS)
        orch = getattr(ctx, "orchestrator", None)
        if orch is None:
            continue
        try:
            await db(release_lost_continuations, ctx.hub_dir, ctx.hub)

            def due():
                with service.engine(ctx.hub_dir).connect() as conn:
                    rows = conn.execute(sa.select(store.requests).where(
                        r.hub_id == ctx.hub, r.origin == "chat", r.continued.is_(False),
                        r.status.in_(("approved", "rejected", "answered"))).limit(20)).fetchall()
                return [store.row_dict(x) for x in rows]

            for req in await db(due):
                await orch.continue_request(req)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - tried again on the next tick
            log.warning("hitl: recovery pass failed", exc_info=True)


def release_lost_continuations(hub_dir, hub: str) -> int:
    """A claim (``continued``) whose continuation message never got written,
    because the process died or the write failed: release it for recovery.
    Only claims older than a minute, so a continuation in flight is left alone."""
    import time

    import sqlalchemy as sa

    from . import store

    r = store.requests.c
    with service.engine(hub_dir).connect() as conn:
        rows = conn.execute(sa.select(r.id, r.conversation_id).where(
            r.hub_id == hub, r.origin == "chat", r.continued.is_(True),
            r.status.in_(("approved", "rejected", "answered")),
            r.decided_at < time.time() - 60, r.decided_at > time.time() - 86400)).fetchall()
        lost = [x.id for x in rows if not conn.execute(sa.text(
            "SELECT 1 FROM hz_messages WHERE conversation_id = :c AND content LIKE :p LIMIT 1"),
            {"c": x.conversation_id, "p": f'%{x.id}%'}).first()]
    if lost:
        with service.engine(hub_dir).begin() as conn:
            conn.execute(store.requests.update().where(r.id.in_(lost)).values(continued=False))
    return len(lost)
