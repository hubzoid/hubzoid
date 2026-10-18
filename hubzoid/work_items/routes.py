"""HTTP routes for work items and operations, per hub.

Mounted in the Console's API (`/portal/api/hubs/{hub}/...`), with its sign-in:
a Console session or an API key. The CLI calls the same service functions.
Every creating POST accepts an `Idempotency-Key` header. Changes accept
`expected_revision`. Errors are `{"detail": {"code", "message", ...}}`.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import config as config_lib
from . import service, store
from .errors import WorkItemError


class StartBody(BaseModel):
    workflow: str = Field(min_length=1, max_length=255)
    key: str = Field(min_length=1, max_length=512)
    route: str | None = Field(default=None, max_length=64)
    input: Any = None
    generation_mode: str = "reuse"


class MessageBody(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    client_message_id: str | None = Field(default=None, max_length=128)
    expected_revision: int | None = None


class CancelBody(BaseModel):
    reason: str = Field(default="", max_length=500)
    expected_revision: int | None = None


class RevisionBody(BaseModel):
    expected_revision: int | None = None


class RestartBody(BaseModel):
    stage: str | None = Field(default=None, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    expected_revision: int | None = None


class ApproveBody(BaseModel):
    digest: str = Field(min_length=8, max_length=80)
    expected_revision: int | None = None


class DenyBody(BaseModel):
    reason: str = Field(default="", max_length=500)
    expected_revision: int | None = None


class ResolveBody(BaseModel):
    decision: str
    evidence: Any = None
    acknowledge_duplicate_risk: bool = False
    expected_revision: int | None = None


def _raise(exc: WorkItemError):
    raise HTTPException(status_code=exc.status, detail=exc.detail())


async def _call(fn: Callable, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except WorkItemError as exc:
        _raise(exc)


def register(router: APIRouter, require_person: Callable, hub_path: Callable[[Any, str], Path]) -> None:
    """Add the routes to `router`. `require_person(request)` returns the
    verified caller (with `.subject` and `.surface`). `hub_path(actor, hub)`
    returns the hub folder or raises 404."""

    def hub_for(actor, hub: str) -> Path:
        try:
            return hub_path(actor, hub)
        except KeyError:
            raise HTTPException(404, detail={"code": "hub_not_found", "message": "There is no such hub here."})

    @router.get("/hubs/{hub}/work-items")
    async def list_items(hub: str, state: str | None = None, workflow: str | None = None,
                         cursor: float | None = None, limit: int = 50, actor=Depends(require_person)):
        return await _call(service.list_items, hub_for(actor, hub), actor.subject, state=state,
                           workflow=workflow, before=cursor, limit=limit)

    @router.post("/hubs/{hub}/work-items")
    async def start(hub: str, request: Request, response: Response, body: StartBody,
                    actor=Depends(require_person)):
        hub_dir = hub_for(actor, hub)

        def produce():
            item, created = service.admit_and_start(
                hub_dir, workflow=body.workflow, key=body.key, route=body.route, input=body.input,
                requester=actor.subject, actor=actor.subject, generation_mode=body.generation_mode,
                origin={"source": "api", "surface": actor.surface})
            return (202 if created else 200), service.show(hub_dir, item["id"], actor.subject)

        status, payload = await _call(store.idempotent, hub_dir, actor.subject, f"{hub}:work_items.start",
                                      request.headers.get("Idempotency-Key"), body.model_dump(), produce)
        response.status_code = status
        payload["status_url"] = f"/portal/api/hubs/{hub}/work-items/{payload['id']}"
        return payload

    @router.get("/hubs/{hub}/work-items/config")
    async def get_config(hub: str, actor=Depends(require_person)):
        hub_dir = hub_for(actor, hub)
        try:
            cfg = await asyncio.to_thread(config_lib.load, hub_dir)
        except WorkItemError as exc:
            _raise(exc)
        return {"revision": cfg.digest or None, "effective": cfg.to_dict(),
                "sources": [config_lib.FILE] if cfg.present else []}

    @router.get("/hubs/{hub}/work-items/{item_id}")
    async def show(hub: str, item_id: str, actor=Depends(require_person)):
        return await _call(service.show, hub_for(actor, hub), item_id, actor.subject)

    @router.post("/hubs/{hub}/work-items/{item_id}/messages", status_code=202)
    async def message(hub: str, item_id: str, body: MessageBody, actor=Depends(require_person)):
        return await _call(service.message, hub_for(actor, hub), item_id, actor.subject, body.message,
                           client_message_id=body.client_message_id,
                           expected_revision=body.expected_revision)

    @router.post("/hubs/{hub}/work-items/{item_id}/cancel", status_code=202)
    async def cancel(hub: str, item_id: str, body: CancelBody = Body(default_factory=CancelBody),
                     actor=Depends(require_person)):
        return await _call(service.cancel, hub_for(actor, hub), item_id, actor.subject, reason=body.reason,
                           surface=actor.surface, expected_revision=body.expected_revision)

    @router.post("/hubs/{hub}/work-items/{item_id}/resume", status_code=202)
    async def resume(hub: str, item_id: str, body: RevisionBody = Body(default_factory=RevisionBody),
                     actor=Depends(require_person)):
        return await _call(service.resume, hub_for(actor, hub), item_id, actor.subject,
                           expected_revision=body.expected_revision, surface=actor.surface)

    @router.post("/hubs/{hub}/work-items/{item_id}/restart", status_code=202)
    async def restart(hub: str, item_id: str, body: RestartBody, actor=Depends(require_person)):
        return await _call(service.restart, hub_for(actor, hub), item_id, actor.subject, stage=body.stage,
                           reason=body.reason, expected_revision=body.expected_revision,
                           surface=actor.surface)

    @router.get("/hubs/{hub}/work-items/{item_id}/operations")
    async def item_operations(hub: str, item_id: str, actor=Depends(require_person)):
        shown = await _call(service.show, hub_for(actor, hub), item_id, actor.subject)
        return {"operations": shown["operations"]}

    @router.get("/hubs/{hub}/operations")
    async def list_operations(hub: str, state: str | None = None, limit: int = 100,
                              actor=Depends(require_person)):
        return await _call(service.list_operations, hub_for(actor, hub), actor.subject, state=state,
                           limit=limit)

    @router.get("/hubs/{hub}/operations/{op_id}")
    async def operation(hub: str, op_id: str, actor=Depends(require_person)):
        return await _call(service.operation_detail, hub_for(actor, hub), op_id, actor.subject)

    @router.post("/hubs/{hub}/operations/{op_id}/approve")
    async def approve(hub: str, op_id: str, body: ApproveBody, actor=Depends(require_person)):
        return await _call(service.approve, hub_for(actor, hub), op_id, actor.subject, digest=body.digest,
                           expected_revision=body.expected_revision, surface=actor.surface)

    @router.post("/hubs/{hub}/operations/{op_id}/deny")
    async def deny(hub: str, op_id: str, body: DenyBody = Body(default_factory=DenyBody),
                   actor=Depends(require_person)):
        return await _call(service.deny, hub_for(actor, hub), op_id, actor.subject, reason=body.reason,
                           expected_revision=body.expected_revision, surface=actor.surface)

    @router.post("/hubs/{hub}/operations/{op_id}/resolve")
    async def resolve(hub: str, op_id: str, body: ResolveBody, actor=Depends(require_person)):
        return await _call(service.resolve, hub_for(actor, hub), op_id, actor.subject,
                           decision=body.decision, evidence=body.evidence,
                           acknowledge_duplicate_risk=body.acknowledge_duplicate_risk,
                           expected_revision=body.expected_revision, surface=actor.surface)


def register_stations(router: APIRouter, require_person: Callable, hub_path: Callable[[Any, str], Path]) -> None:
    """Station calls and handoffs of a work item, read-only. Calls are made by
    the work item's workflow; their single retry is directed by it too."""
    from ..stations import service as station_service

    def hub_for(actor, hub: str) -> Path:
        try:
            return hub_path(actor, hub)
        except KeyError:
            raise HTTPException(404, detail={"code": "hub_not_found", "message": "There is no such hub here."})

    @router.get("/hubs/{hub}/work-items/{item_id}/stations")
    async def stations(hub: str, item_id: str, actor=Depends(require_person)):
        return await _call(station_service.view_for, hub_for(actor, hub), item_id, actor.subject)

    @router.get("/hubs/{hub}/work-items/{item_id}/handoffs/{doc_id}")
    async def handoff(hub: str, item_id: str, doc_id: str, actor=Depends(require_person)):
        hub_dir = hub_for(actor, hub)
        view = await _call(station_service.view_for, hub_dir, item_id, actor.subject)
        if not any(h["id"] == doc_id and "title" in h for h in view["handoffs"]):
            raise HTTPException(404, detail={"code": "not_found", "message": "No such handoff here, or you can't see it."})
        return await _call(station_service.read_handoff, hub_dir, item_id, doc_id)
