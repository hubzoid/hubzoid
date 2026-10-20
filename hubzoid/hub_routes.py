"""Hub-scoped API routes: ``/api/hubs/{hub_id}/...``.

A hub's ID is its hub key, the lowercase name access grants use
(``assertions.hub_key``), never the gateway's ``/b/<slug>``. In a gateway the
edge sends ``/api/hubs/<key>/*`` to that hub's bridge (``gateway.edge_routes``).
A bridge answers only for its own hub and returns 404 ``hub_not_found`` for any
other ID, without saying whether that hub exists.

A feature adds its hub-scoped routes to the router this returns:

    router = hub_routes.router(hub_dir)

    @router.get("/secrets")
    def list_secrets(hub_id: str, request: Request): ...

    app.include_router(router)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends

from .access.identity import normalize
from .assertions import hub_key
from .chat.common import error

PREFIX = "/api/hubs/{hub_id}"


def check(hub_dir: Path):
    """A dependency that refuses any hub ID but this bridge's own."""
    own = hub_key(hub_dir)

    def _check(hub_id: str) -> str:
        if normalize(hub_id) != own:
            raise error(404, "hub_not_found", "No hub with this ID is served here.")
        return own

    return _check


def router(hub_dir: Path, **kwargs) -> APIRouter:
    """An APIRouter under ``/api/hubs/{hub_id}`` that only this hub's ID reaches."""
    deps = [Depends(check(Path(hub_dir))), *kwargs.pop("dependencies", [])]
    return APIRouter(prefix=PREFIX, dependencies=deps, **kwargs)
