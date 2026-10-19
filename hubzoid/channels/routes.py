"""HTTP for channels (mounted by ``hubzoid.webapp``), under
``/api/hubs/{hub_id}/channels``.

Management (signed in, the hub's administrators; writes need a same-origin
request and accept an ``Idempotency-Key``):

    GET    /                                  connections with status and counts
    POST   /validate                          {config} or {text}: no activation
    GET    /{id}/config                       the active configuration (references only)
    PUT    /{id}/config                       {expected_revision, config | text}
    POST   /{id}/enabled                      {enabled, expected_revision}: pause or resume
    POST   /{id}/doctor                       {actor?, workspace_id?, ...}: read-only checks
    GET    /{id}/deliveries                   ?status&thread&cursor&limit
    POST   /{id}/deliveries/{delivery}/redrive
    GET    /{id}/outbox                       ?state&thread
    POST   /{id}/outbox/{operation}/resolve   {decision: check|done|retry|abandon, ...}
    GET    /{id}/threads                      ?cursor&limit
    GET    /{id}/threads/{thread}
    PUT    /{id}/threads/{thread}/subscription  {subscribed, expected_revision}
    GET    /{id}/links
    POST   /{id}/links                        {external_id, account, workspace_id?, display?}
    DELETE /{id}/links/{link}

Provider intake (no sign-in; the adapter verifies the provider's signature):

    GET|POST /{id}/webhook

Errors are ``{"detail": {"code", "message", ...}}``. A delivery is answered
only after it is saved; a storage failure is a 503 the provider retries.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .. import hub_routes
from ..control import authz
from . import admission, doctor, links, replies, router as router_lib, store
from . import config as config_lib
from .errors import ChannelError
from .models import Challenge

log = logging.getLogger("hubzoid.channels.routes")
_NO_STORE = {"Cache-Control": "no-store"}


def _http(exc: ChannelError):
    return exc.http()


async def _json(request: Request) -> dict:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise ChannelError("invalid_request", "Send the request body as JSON.", status=400).http() from None
    if not isinstance(data, dict):
        raise ChannelError("invalid_request", "Send a JSON object.", status=400).http()
    return data


def _revision(raw):
    if raw is None or raw == "":
        return None
    if raw == "absent":
        return "absent"
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ChannelError("validation_failed", 'expected_revision is a number or "absent".',
                           status=422).http() from None


async def call(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except ChannelError as exc:
        raise exc.http() from None
    except Exception as exc:  # noqa: BLE001 - a WorkItemError from the operation ledger
        code = getattr(exc, "code", None)
        if code and hasattr(exc, "status"):
            raise ChannelError(code, getattr(exc, "message", str(exc)), status=exc.status).http() from None
        raise


async def _read_limited(request: Request, limit: int) -> bytes:
    """The raw body, refused while streaming once it passes `limit`."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ChannelError("delivery_too_large", "The request body is larger than this channel accepts.",
                               status=413).http()
        chunks.append(chunk)
    return b"".join(chunks)


def summary(hub_dir: Path, row: dict) -> dict:
    counts = store.counts(hub_dir, row["hub"], row["id"])
    deliveries = counts["deliveries"]
    try:
        uncertain = len(replies.outbox(hub_dir, row["hub"], row["id"], state="ambiguous", limit=200))
    except Exception:  # noqa: BLE001
        uncertain = None
    return {"id": row["id"], "provider": row["provider"], "status": row["status"], "revision": row["revision"],
            "target": (f"agent:{row['config_json'].get('agent')}" if row["config_json"].get("agent")
                       else f"workflow:{row['config_json'].get('workflow')}"),
            "threads": counts["threads"], "last_delivery_at": counts["last_delivery_at"],
            "backlog": deliveries.get("accepted", 0) + deliveries.get("waiting", 0) + deliveries.get("routing", 0),
            "quarantined": deliveries.get("quarantined", 0), "denied": deliveries.get("denied", 0),
            "uncertain_replies": uncertain, "updated": row["updated"]}


def list_all(hub_dir: Path) -> dict:
    hub = config_lib.hub_of(hub_dir)
    rows = store.list_connections(hub_dir, hub)
    items = [summary(hub_dir, r) for r in rows]
    applied = {r["id"] for r in rows}
    unapplied = []
    for path in config_lib.files(hub_dir):
        if path.stem not in applied:
            unapplied.append({"id": path.stem, "file": str(path.relative_to(hub_dir)), "status": "not_applied"})
    return {"items": items, "not_applied": unapplied}


def thread_detail(hub_dir: Path, hub: str, cid: str, tid: str) -> dict:
    row = store.require_thread(hub_dir, hub, cid, tid)
    deliveries = store.list_deliveries(hub_dir, hub, cid, thread=tid, limit=100)["items"]
    out = {**thread_view(row), "deliveries": [delivery_view(d) for d in deliveries],
           "replies": replies.outbox(hub_dir, hub, cid, thread=tid, limit=100)}
    if row.get("work_item_id"):
        from ..work_items import store as wstore

        item = wstore.get_item(hub_dir, row["work_item_id"])
        if item is not None:
            out["work_item"] = {"id": item["id"], "state": item["state"], "workflow": item["workflow"],
                                "requester": item.get("requester")}
    return out


def thread_view(row: dict) -> dict:
    return {"id": row["id"], "workspace_id": row["workspace_id"], "thread_key": row["thread_key"],
            "origin": row["origin_json"], "subscribed": bool(row["subscribed"]),
            "conversations": row.get("conversations_json") or {}, "work_item_id": row.get("work_item_id"),
            "requester": row.get("requester"), "revision": row["revision"], "created": row["created"],
            "updated": row["updated"]}


def delivery_view(row: dict) -> dict:
    return {"id": row["id"], "delivery_id": row["delivery_id"], "event_index": row["event_index"],
            "kind": row["kind"], "status": row["status"], "reason": row["reason"], "thread_id": row["thread_id"],
            "actor": row.get("actor_json") or {}, "route": row.get("route_json") or {},
            "verification": row["verification"], "created": row["created"], "updated": row["updated"]}


def redrive(hub_dir: Path, hub: str, cid: str, did: str, actor: str) -> dict:
    row = store.get_delivery(hub_dir, did)
    if row is None or row["hub"] != hub or row["connection_id"] != cid:
        raise ChannelError("not_found", "No delivery with that ID on this channel.", status=404)
    if row["status"] in ("routed", "routing", "accepted", "waiting"):
        raise ChannelError("already_routed", f"This delivery is {row['status']}. Only a failed, denied or paused "
                           "one can be redriven.", status=409)
    if row["status"] in ("duplicate", "quarantined") or not row.get("event_enc"):
        raise ChannelError("not_redrivable", "A duplicate or quarantined delivery never runs.", status=409)
    out = store.update_delivery(hub_dir, did, expect_status=row["status"], status="accepted", reason=None)
    if out is None:
        raise ChannelError("revision_conflict", "The delivery changed. Reload and try again.", status=409)
    config_lib.audit(hub_dir, actor, "channel_redrive", did)
    router_lib.kick(hub_dir)
    return delivery_view(out)


def mount(app, hub_dir: Path, **_ctx) -> None:
    hub_dir = Path(hub_dir)
    router = hub_routes.router(hub_dir, tags=["channels"])

    async def person(request: Request, capability: str, *, mutation: bool = False):
        return await authz.require_request(request, hub_dir, capability, config_lib.hub_of(hub_dir),
                                           mutation=mutation)

    async def conn_of(cid: str):
        return await call(config_lib.active, hub_dir, cid)

    @router.get("/channels")
    async def list_channels(hub_id: str, request: Request):
        await person(request, "channels.inspect")
        return JSONResponse(await call(list_all, hub_dir), headers=_NO_STORE)

    @router.post("/channels/validate")
    async def validate(hub_id: str, request: Request):
        await person(request, "channels.manage")
        body = await _json(request)
        if isinstance(body.get("text"), str):
            result = await call(config_lib.load_text, hub_dir, body["text"])
        else:
            result = await call(config_lib.validate, hub_dir, body.get("config") or {})
        return result.to_json()

    @router.get("/channels/{cid}/config")
    async def get_config(hub_id: str, cid: str, request: Request):
        await person(request, "channels.inspect")
        return JSONResponse(await call(config_lib.export, hub_dir, cid), headers=_NO_STORE)

    @router.put("/channels/{cid}/config")
    async def put_config(hub_id: str, cid: str, request: Request):
        p = await person(request, "channels.manage", mutation=True)
        body = await _json(request)
        expected = _revision(body.get("expected_revision"))
        if expected is None:
            raise ChannelError("validation_failed", 'Send expected_revision (a number, or "absent" to create).',
                               status=422).http()
        if isinstance(body.get("text"), str):
            v = await call(config_lib.load_text, hub_dir, body["text"], expected_id=cid)
            if not v.valid:
                raise ChannelError("validation_failed", "The channel configuration is not valid.", status=422,
                                   fields=v.errors).http()
            out = await call(config_lib.apply, hub_dir, v.data, expected_revision=expected, actor=p.email,
                             text=body["text"], expected_id=cid)
        else:
            out = await call(config_lib.apply, hub_dir, body.get("config") or {}, expected_revision=expected,
                             actor=p.email, expected_id=cid)
        await router_lib.reload_transports()
        router_lib.kick(hub_dir)
        return JSONResponse(out, status_code=201 if out["created"] else 200)

    @router.post("/channels/{cid}/enabled")
    async def set_enabled(hub_id: str, cid: str, request: Request):
        p = await person(request, "channels.manage", mutation=True)
        body = await _json(request)
        expected = _revision(body.get("expected_revision"))
        if not isinstance(body.get("enabled"), bool) or not isinstance(expected, int):
            raise ChannelError("validation_failed", "Send enabled (true or false) and expected_revision.",
                               status=422).http()
        out = await call(config_lib.set_enabled, hub_dir, cid, body["enabled"], expected_revision=expected,
                         actor=p.email)
        await router_lib.reload_transports()
        router_lib.kick(hub_dir)
        return out

    @router.post("/channels/{cid}/doctor")
    async def run_doctor(hub_id: str, cid: str, request: Request):
        await person(request, "channels.inspect")
        body = await _json(request)
        conn = await conn_of(cid)
        return JSONResponse(await call(doctor.run, conn, body), headers=_NO_STORE)

    @router.get("/channels/{cid}/deliveries")
    async def deliveries(hub_id: str, cid: str, request: Request, status: str | None = None,
                         thread: str | None = None, cursor: str | None = None, limit: int = 50):
        await person(request, "channels.inspect")
        await conn_of(cid)
        out = await call(store.list_deliveries, hub_dir, config_lib.hub_of(hub_dir), cid, status=status,
                         thread=thread, limit=limit, cursor=cursor)
        return {"items": [delivery_view(d) for d in out["items"]], "next_cursor": out["next_cursor"]}

    @router.post("/channels/{cid}/deliveries/{did}/redrive", status_code=202)
    async def redrive_delivery(hub_id: str, cid: str, did: str, request: Request):
        p = await person(request, "channels.manage", mutation=True)
        return await call(redrive, hub_dir, config_lib.hub_of(hub_dir), cid, did, p.email)

    @router.get("/channels/{cid}/outbox")
    async def outbox(hub_id: str, cid: str, request: Request, state: str | None = None,
                     thread: str | None = None, limit: int = 50):
        await person(request, "channels.inspect")
        await conn_of(cid)
        return {"items": await call(replies.outbox, hub_dir, config_lib.hub_of(hub_dir), cid, state=state,
                                    thread=thread, limit=limit)}

    @router.post("/channels/{cid}/outbox/{op_id}/resolve")
    async def resolve(hub_id: str, cid: str, op_id: str, request: Request):
        p = await person(request, "channels.manage", mutation=True)
        body = await _json(request)
        decision = body.get("decision")
        if decision not in ("check", "done", "retry", "abandon"):
            raise ChannelError("validation_failed", "decision is check, done, retry or abandon.", status=422).http()
        conn = await conn_of(cid)
        out = await call(replies.resolve, conn, op_id, decision=decision, actor=p.email,
                         evidence=body.get("evidence"),
                         acknowledge_duplicate_risk=body.get("acknowledge_duplicate_risk") is True)
        config_lib.audit(hub_dir, p.email, f"channel_reply_{decision}", op_id)
        return out

    @router.get("/channels/{cid}/threads")
    async def threads(hub_id: str, cid: str, request: Request, cursor: str | None = None, limit: int = 50):
        await person(request, "channels.inspect")
        await conn_of(cid)
        out = await call(store.list_threads, hub_dir, config_lib.hub_of(hub_dir), cid, limit=limit, cursor=cursor)
        return {"items": [thread_view(t) for t in out["items"]], "next_cursor": out["next_cursor"]}

    @router.get("/channels/{cid}/threads/{tid}")
    async def thread(hub_id: str, cid: str, tid: str, request: Request):
        await person(request, "channels.inspect")
        return JSONResponse(await call(thread_detail, hub_dir, config_lib.hub_of(hub_dir), cid, tid),
                            headers=_NO_STORE)

    @router.put("/channels/{cid}/threads/{tid}/subscription")
    async def subscription(hub_id: str, cid: str, tid: str, request: Request):
        p = await person(request, "channels.manage", mutation=True)
        body = await _json(request)
        if not isinstance(body.get("subscribed"), bool):
            raise ChannelError("validation_failed", "Send subscribed: true or false.", status=422).http()
        hub = config_lib.hub_of(hub_dir)
        await call(store.require_thread, hub_dir, hub, cid, tid)
        row = await call(store.update_thread, hub_dir, tid, expected_revision=_revision(body.get("expected_revision")),
                         subscribed=1 if body["subscribed"] else 0)
        config_lib.audit(hub_dir, p.email, "channel_subscribe" if body["subscribed"] else "channel_unsubscribe", tid)
        return thread_view(row)

    @router.get("/channels/{cid}/links")
    async def get_links(hub_id: str, cid: str, request: Request):
        await person(request, "channels.inspect")
        conn = await conn_of(cid)
        return {"items": await call(links.links_for, conn)}

    @router.post("/channels/{cid}/links", status_code=201)
    async def add_link(hub_id: str, cid: str, request: Request):
        p = await person(request, "channels.manage", mutation=True)
        body = await _json(request)
        conn = await conn_of(cid)
        out = await call(links.link, conn, external_id=str(body.get("external_id") or ""),
                         account=str(body.get("account") or ""), workspace_id=body.get("workspace_id"),
                         display=str(body.get("display") or ""), actor=p.email)
        router_lib.kick(hub_dir)
        return out

    @router.delete("/channels/{cid}/links/{lid}", status_code=204)
    async def remove_link(hub_id: str, cid: str, lid: str, request: Request):
        p = await person(request, "channels.manage", mutation=True)
        await conn_of(cid)
        await call(links.unlink, hub_dir, lid, actor=p.email)
        return Response(status_code=204)

    async def webhook(hub_id: str, cid: str, request: Request):
        try:
            conn = await asyncio.to_thread(config_lib.active, hub_dir, cid)
            raw = await _read_limited(request, conn.adapter.body_limit)
            result = await asyncio.to_thread(admission.receive, hub_dir, cid, raw, dict(request.headers),
                                             dict(request.query_params))
        except ChannelError as exc:
            if exc.status >= 500:
                log.warning("channel %s: delivery refused: %s", cid, exc.code)
            return JSONResponse({"detail": exc.detail()}, status_code=exc.status, headers=_NO_STORE)
        if isinstance(result, Challenge):
            if result.media_type == "text/plain":
                return PlainTextResponse(str(result.body or ""), status_code=result.status)
            return JSONResponse(result.body, status_code=result.status)
        status, body = conn.adapter.ack_response(result)
        if not isinstance(body, (dict, list)):
            return PlainTextResponse(str(body or ""), status_code=status)
        return JSONResponse(body, status_code=status, headers=_NO_STORE)

    router.add_api_route("/channels/{cid}/webhook", webhook, methods=["GET", "POST"], include_in_schema=True)

    from . import registry

    for name in registry.known():
        try:
            registry.get(name).routes(router, hub_dir)
        except Exception:  # noqa: BLE001 - a provider without its optional library adds no routes
            log.debug("channels: provider %s adds no routes", name, exc_info=True)
    app.include_router(router)
