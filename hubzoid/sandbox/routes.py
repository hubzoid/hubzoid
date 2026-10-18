"""HTTP routes for sandboxes, per hub, in the Console's API
(`/portal/api/hubs/{hub}/sandboxes...`). The CLI calls the same functions.

Reading sandbox metadata needs `workflows_view`; anything that creates, stops
or deletes a sandbox, or runs a probe, needs `workflows_manage`. Neither shows
a sandbox's files or command output. Errors are `{"detail": {"code",
"message", ...}}`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from ..work_items.errors import WorkItemError
from ..work_items.routes import _call


class ReasonBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    expected_revision: int | None = None


class ProbeBody(BaseModel):
    profile: str = Field(min_length=1, max_length=64)
    argv: list[str] = Field(min_length=1, max_length=64)


class ReapBody(BaseModel):
    dry_run: bool = True


def register(router: APIRouter, require_person: Callable, hub_path: Callable[[Any, str], Path]) -> None:
    from ..work_items import service as wi_service
    from . import config, service

    def hub_for(actor, hub: str, permission: str) -> Path:
        try:
            hub_dir = hub_path(actor, hub)
        except KeyError:
            raise HTTPException(404, detail={"code": "hub_not_found", "message": "There is no such hub here."})
        if not (wi_service._can(hub_dir, actor.subject, permission)
                or wi_service._can(hub_dir, actor.subject, "workflows_manage")):
            raise HTTPException(403, detail={"code": "permission_denied",
                                             "message": "Sandboxes are for the hub's managers."})
        return hub_dir

    @router.get("/hubs/{hub}/sandboxes")
    async def list_sandboxes(hub: str, state: str | None = None, owner_kind: str | None = None, limit: int = 50,
                             actor=Depends(require_person)):
        return await _call(service.list_view, hub_for(actor, hub, "workflows_view"), state=state,
                           owner_kind=owner_kind, limit=limit)

    @router.post("/hubs/{hub}/sandboxes/doctor")
    async def doctor(hub: str, actor=Depends(require_person)):
        return await _call(service.doctor, hub_for(actor, hub, "workflows_view"))

    @router.get("/hubs/{hub}/sandboxes/profiles")
    async def profiles(hub: str, actor=Depends(require_person)):
        hub_dir = hub_for(actor, hub, "workflows_view")

        def build():
            out = []
            for pid in config.list_profiles(hub_dir):
                try:
                    p = config.load_profile(hub_dir, pid)
                    out.append(dict(p.to_json(), valid=True, setup_hosts=p.setup_hosts() if p.setup else []))
                except WorkItemError as exc:
                    out.append({"id": pid, "valid": False, "error": exc.message})
            return {"profiles": out}

        return await _call(build)

    @router.post("/hubs/{hub}/sandboxes/profiles/{profile}/prepare")
    async def prepare(hub: str, profile: str, actor=Depends(require_person)):
        return await _call(service.prepare, hub_for(actor, hub, "workflows_manage"), profile)

    @router.post("/hubs/{hub}/sandboxes/probes")
    async def probe(hub: str, body: ProbeBody, actor=Depends(require_person)):
        return await _call(service.probe, hub_for(actor, hub, "workflows_manage"), body.profile, body.argv,
                           actor=actor.subject)

    @router.post("/hubs/{hub}/sandboxes/reaper")
    async def reaper(hub: str, body: ReapBody = Body(default_factory=ReapBody), actor=Depends(require_person)):
        hub_dir = hub_for(actor, hub, "workflows_manage" if not body.dry_run else "workflows_view")
        return await _call(service.reap, hub_dir, dry_run=body.dry_run)

    @router.get("/hubs/{hub}/sandboxes/{sandbox_id}")
    async def show(hub: str, sandbox_id: str, actor=Depends(require_person)):
        hub_dir = hub_for(actor, hub, "workflows_view")
        return service.view(await _call(service.require, hub_dir, sandbox_id))

    @router.post("/hubs/{hub}/sandboxes/{sandbox_id}/stop")
    async def stop(hub: str, sandbox_id: str, body: ReasonBody, actor=Depends(require_person)):
        return await _call(service.operator_stop, hub_for(actor, hub, "workflows_manage"), sandbox_id,
                           actor.subject, reason=body.reason, expected_revision=body.expected_revision)

    @router.delete("/hubs/{hub}/sandboxes/{sandbox_id}")
    async def delete(hub: str, sandbox_id: str, body: ReasonBody, actor=Depends(require_person)):
        return await _call(service.operator_delete, hub_for(actor, hub, "workflows_manage"), sandbox_id,
                           actor.subject, reason=body.reason, expected_revision=body.expected_revision)
