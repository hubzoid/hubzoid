"""HTTP for the secret store (mounted by ``hubzoid.webapp``).

  One hub (organization administrators write, the hub's administrators read)
    GET    /api/hubs/{hub_id}/secrets            every row: name, level, status, source,
                                                 lock, stored metadata. Never a value.
    PUT    /api/hubs/{hub_id}/secrets/{name}     {value, restricted?, expected_revision?}
    DELETE /api/hubs/{hub_id}/secrets/{name}     ?expected_revision=N

  The deployment (organization administrators)
    GET    /api/secrets                          stored deployment secrets
    PUT    /api/secrets/{name}                   {value, expected_revision?}
    DELETE /api/secrets/{name}                   ?expected_revision=N

A value is never returned. A saved value applies after the bridge restarts,
and every write answers ``restart_required: true``. A name an operator source
sets (environment, ``.env``, ``restricted/.env`` or an AWS secret) is locked:
409 ``secret_locked``. An agent administrator sees a count of the hub's
restricted secrets, not their names, and the server refuses them the rest.

Errors are ``{"detail": {"code", "message", ...}}``. Every write needs a
same-origin request (``access.session.require_same_origin``).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .. import hub_access, hub_routes
from ..hub_access import Viewer
from ..access.identity import normalize
from ..assertions import hub_key
from ..chat.common import error
from . import layer as layer_lib
from . import names, report, store
from .store import SecretError

log = logging.getLogger("hubzoid.secretstore")

_NO_STORE = {"Cache-Control": "no-store"}


def _raise(err: SecretError):
    raise error(err.status, err.code, err.message, **err.extra)


async def _body(request: Request) -> dict:
    import json

    raw = await request.body()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise error(422, "invalid_request", "Send the request body as JSON.") from None
    if not isinstance(data, dict):
        raise error(422, "invalid_request", "Send a JSON object.")
    return data


def _expected_revision(raw: Any):
    if raw is None or raw == "":
        return None
    if raw == "absent":
        return "absent"
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise error(422, "validation_failed", "expected_revision is a number or \"absent\".",
                    fields={"expected_revision": "invalid"}) from None
    if value < 1:
        raise error(422, "validation_failed", "expected_revision starts at 1.",
                    fields={"expected_revision": "invalid"})
    return value


def build_router(hub_dir: Path) -> list[APIRouter]:
    hub_dir = Path(hub_dir)
    own = hub_key(hub_dir)
    hub_router = hub_routes.router(hub_dir, tags=["secrets"])
    deployment_router = APIRouter(prefix="/api/secrets", tags=["secrets"])

    # ---- who: organization administrators write, this hub's administrators read ----
    def reader(request: Request) -> Viewer:
        return hub_access.hub_admin(request, hub_dir)

    def writer(request: Request) -> Viewer:
        who = hub_access.viewer(request, hub_dir)
        if not who.org_admin:
            raise error(403, "permission_denied", "Only organization administrators set or remove secrets.")
        hub_access.same_origin(request)
        return who

    def viewer(request: Request) -> Viewer:
        return hub_access.viewer(request, hub_dir)

    request_id = hub_access.request_id

    def rows(*, strict: bool = False) -> list[dict]:
        """The report. ``strict`` (before a write) refuses rather than guess when
        an AWS secret that might own the name cannot be read."""
        from .. import config_secrets as cs

        try:
            return report.rows(hub_dir, running=layer_lib.active(hub_dir), strict=strict)
        except cs.SecretFetchError:
            raise error(503, "unavailable", "An AWS secret of this hub cannot be read, so it is not "
                                            "known who sets this name. Run hubzoid doctor.") from None

    # ---- one hub --------------------------------------------------------------
    @hub_router.get("/secrets")
    def list_hub(request: Request, who: Viewer = Depends(reader)):
        view = report.visible(rows(), restricted_names=who.org_admin)
        return JSONResponse({"hub": own, **view, "can_manage": who.org_admin,
                             "applies": "after_restart"}, headers=_NO_STORE)

    @hub_router.put("/secrets/{name}")
    async def set_hub(name: str, request: Request, who: Viewer = Depends(writer)):
        body = await _body(request)
        restricted = body.get("restricted")  # omitted keeps a stored secret's restriction
        if restricted is not None and not isinstance(restricted, bool):
            raise error(422, "validation_failed", "restricted is true or false.", fields={"restricted": "invalid"})
        row = next((r for r in rows(strict=True) if r["name"] == name), None)
        if row and row["locked"]:
            raise error(409, "secret_locked", row["lock_reason"] or "An operator source sets this secret.")
        try:
            meta = store.put(hub_dir, scope="hub", target=own, name=name, value=body.get("value"),
                             actor=who.email, restricted=restricted,
                             destination=names.destination(name, os.environ),
                             expected_revision=_expected_revision(body.get("expected_revision")),
                             request_id=request_id(request))
        except SecretError as err:
            _raise(err)
        return JSONResponse({"secret": meta.public(), "restart_required": True}, headers=_NO_STORE)

    @hub_router.delete("/secrets/{name}")
    def delete_hub(name: str, request: Request, expected_revision: str | None = None,
                   who: Viewer = Depends(writer)):
        try:
            meta = store.delete(hub_dir, scope="hub", target=own, name=name, actor=who.email,
                                expected_revision=_expected_revision(expected_revision) if expected_revision
                                else None, request_id=request_id(request))
        except SecretError as err:
            _raise(err)
        return JSONResponse({"deleted": meta.public(), "restart_required": True}, headers=_NO_STORE)

    # ---- the deployment ---------------------------------------------------------
    def org_reader(request: Request) -> Viewer:
        who = viewer(request)
        if not who.org_admin:
            raise error(403, "permission_denied", "Only organization administrators see deployment secrets.")
        return who

    @deployment_router.get("")
    def list_deployment(request: Request, who: Viewer = Depends(org_reader)):
        stored = [m.public() for m in store.list_meta(hub_dir, scope="deployment")]
        return JSONResponse({"secrets": stored, "can_manage": True, "applies": "after_restart"},
                            headers=_NO_STORE)

    @deployment_router.put("/{name}")
    async def set_deployment(name: str, request: Request, who: Viewer = Depends(writer)):
        body = await _body(request)
        if body.get("restricted"):
            raise error(422, "invalid_request", "Only a hub secret can be restricted.")
        row = next((r for r in rows(strict=True) if r["name"] == name and r["level"] == "deployment"), None)
        if row and row["locked"]:
            raise error(409, "secret_locked", row["lock_reason"] or "An operator source sets this secret.")
        try:
            meta = store.put(hub_dir, scope="deployment", name=name, value=body.get("value"),
                             actor=who.email, destination=names.destination(name, os.environ),
                             expected_revision=_expected_revision(body.get("expected_revision")),
                             request_id=request_id(request))
        except SecretError as err:
            _raise(err)
        return JSONResponse({"secret": meta.public(), "restart_required": True}, headers=_NO_STORE)

    @deployment_router.delete("/{name}")
    def delete_deployment(name: str, request: Request, expected_revision: str | None = None,
                          who: Viewer = Depends(writer)):
        try:
            meta = store.delete(hub_dir, scope="deployment", name=name, actor=who.email,
                                expected_revision=_expected_revision(expected_revision) if expected_revision
                                else None, request_id=request_id(request))
        except SecretError as err:
            _raise(err)
        return JSONResponse({"deleted": meta.public(), "restart_required": True}, headers=_NO_STORE)

    return [hub_router, deployment_router]


def mount(app, hub_dir, **ctx) -> None:  # noqa: ARG001 - webapp passes runtime context
    for router in build_router(Path(hub_dir)):
        app.include_router(router)
