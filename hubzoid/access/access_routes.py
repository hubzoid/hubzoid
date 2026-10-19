# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""Read-only access routes for the web app (mounted by ``hubzoid.webapp``).

  GET /api/me/access          what the signed-in person can use, and why
  GET /api/run-identity       who a run is for (``?kind=chat&id=<message id>``)
  GET /api/openapi.json       the API reference, for signed-in people

Every answer comes from the one access check (``access.evaluator``) and the
run identity records (``access.run_identity``); nothing here changes access.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .identity import normalize

log = logging.getLogger("hubzoid.access")
_NO_STORE = {"Cache-Control": "no-store"}


def my_access(hub_dir: Path, user) -> dict:
    """The person's agents and chat features, with plain reasons, and who to ask."""
    from .. import capabilities
    from ..chat import features
    from ..webapp_gateway import agents_for
    from . import store_for

    agents = agents_for(hub_dir, user.email)
    if user.hubs is not None:
        agents = [a for a in agents if normalize(a.get("hub") or "") in user.hubs]
    out_agents = []
    for card in agents:
        hub = normalize(card.get("hub") or "")
        path = Path(card.get("path") or hub_dir) if card.get("path") else hub_dir
        try:
            granted = sorted(store_for(hub_dir).permissions_for(normalize(user.email), hub))
        except Exception:  # noqa: BLE001 - shown as unknown, never as more
            granted = []
        labels = []
        try:
            catalog = {e["permission"]: e for e in capabilities.catalog(path)}
        except Exception:  # noqa: BLE001
            catalog = {}
        for perm in granted:
            if perm in ("use_hub",):
                continue
            entry = catalog.get(perm) or {}
            labels.append({"permission": perm, "label": entry.get("label") or perm})
        out_agents.append({"hub": hub, "name": card.get("name") or hub, "extra": labels})
    try:
        feats = features.for_person(hub_dir, user.email)
    except Exception:  # noqa: BLE001
        feats = {}
    feature_list = [{"key": key, "label": label, "on": bool(feats.get(key))}
                    for key, (label, _desc) in features.FEATURES.items()]
    contact = _admin_contact(hub_dir)
    return {"agents": out_agents, "features": feature_list, "contact": contact,
            "role": user.role, "via_key": user.via_key}


def _admin_contact(hub_dir: Path) -> str:
    import os

    explicit = (os.environ.get("HUBZOID_SUPPORT_CONTACT") or "").strip()
    if explicit:
        return explicit
    return "your administrator"


def mount(app: FastAPI, hub_dir: Path, **_ctx) -> None:
    hub_dir = Path(hub_dir)
    from .. import auth

    @app.get("/api/me/access")
    async def me_access(request: Request):
        user = await run_in_threadpool(auth.require_user, request, hub_dir)
        try:
            body = await run_in_threadpool(my_access, hub_dir, user)
        except Exception:  # noqa: BLE001 - fail closed: say it, never guess more access
            log.exception("access: my access unavailable")
            raise HTTPException(503, detail={"code": "access_unavailable",
                                             "message": "We could not check your access. Try again."})
        return JSONResponse(body, headers=_NO_STORE)

    @app.get("/api/run-identity")
    async def run_identity_view(request: Request, kind: str, id: str):  # noqa: A002
        from . import run_identity

        user = await run_in_threadpool(auth.require_user, request, hub_dir)
        record = await run_in_threadpool(run_identity.for_run, hub_dir, kind, id)
        if record is None:
            return JSONResponse({"identity": None, "summary": run_identity.describe(None)},
                                headers=_NO_STORE)
        mine = user.id in (record.get("acts_as"),) or normalize(user.email) in (
            normalize(record.get("acts_as_email") or ""), normalize(record.get("requested_by") or ""))
        if not (mine or user.is_admin):
            raise HTTPException(404, detail={"code": "not_found", "message": "Not found."})
        names = _names(hub_dir, record)
        public = {k: record.get(k) for k in (
            "id", "run_kind", "hub", "acts_as_email", "requested_by", "requested_by_kind",
            "started_from", "origin_id", "person_present", "credential_kind", "legacy",
            "created_at", "parent_id")}
        return JSONResponse({"identity": public, "summary": run_identity.describe(record, names)},
                            headers=_NO_STORE)

    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi_doc(request: Request):
        await run_in_threadpool(auth.require_user, request, hub_dir)
        return JSONResponse(app.openapi(), headers=_NO_STORE)


def _names(hub_dir: Path, record: dict) -> dict:
    from ..auth import users

    out = {}
    for email in {record.get("acts_as_email"), record.get("requested_by")}:
        if not email:
            continue
        row = users.find_by_email(hub_dir, email)
        if row and row.get("name"):
            out[email] = row["name"]
    return out
