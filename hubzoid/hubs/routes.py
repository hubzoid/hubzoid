"""HTTP for a hub's declared settings and its own checks (mounted by ``hubzoid.webapp``).

    GET  /api/hubs/{hub_id}/settings            every declared setting, its value or
                                                reference, source and status, and the revision
    GET  /api/hubs/{hub_id}/settings/schema     the declarations
    POST /api/hubs/{hub_id}/settings/validate   {version: 1, values, secrets} -> every problem
    PUT  /api/hubs/{hub_id}/settings            the same body with expected_revision -> written
    POST /api/hubs/{hub_id}/doctor              run the hub's own checks now

This hub's administrators read, change values and run checks. Changing a
secret reference needs an organization administrator, because a reference
decides which stored secret the hub's code receives. A secret value is never
accepted or returned here. A saved change applies after the hub restarts.

Errors are ``{"detail": {"code", "message", ...}}``. Writes need the site's own
origin.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import hub_access, hub_routes
from ..chat.common import error
from . import settings as hs
from .yamlfile import HubFileError

_NO_STORE = {"Cache-Control": "no-store"}
DOCTOR_EVERY = 10.0  # seconds between two runs of a hub's checks


async def _document(request: Request) -> tuple[dict, object]:
    import json

    raw = await request.body()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise error(422, "invalid_request", "Send the request body as JSON.") from None
    if not isinstance(data, dict):
        raise error(422, "invalid_request", "Send a JSON object.")
    expected = data.pop("expected_revision", None)
    data.setdefault("version", 1)
    return data, expected


def build_router(hub_dir: Path):
    hub_dir = Path(hub_dir)
    router = hub_routes.router(hub_dir, tags=["settings"])
    doctor_lock = threading.Lock()
    last_run = {"at": 0.0}

    def candidate(document: dict) -> hs.ValuesFile:
        try:
            return hs.parse_values(document)
        except HubFileError as exc:
            raise error(422, "validation_failed", str(exc).replace(f"{hs.VALUES}: ", "")) from None

    def problems_of(values: hs.ValuesFile) -> tuple[list, list]:
        try:
            resolved, problems = hs.validate(hub_dir, values)
        except HubFileError as exc:
            raise error(422, "validation_failed", str(exc)) from None
        return resolved, problems

    def changed(values: hs.ValuesFile) -> list[str]:
        try:
            current = hs.values_file(hub_dir)
        except HubFileError:
            current = None
        before = {**(current.values if current else {}), **(current.secrets if current else {})}
        after = {**values.values, **values.secrets}
        return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))

    @router.get("/settings")
    def read(request: Request):
        who = hub_access.hub_admin(request, hub_dir)
        try:
            resolved, problems = hs.validate(hub_dir)
        except HubFileError as exc:
            raise error(422, "validation_failed", str(exc)) from None
        return JSONResponse({"revision": hs.revision(hub_dir), "settings": [r.public() for r in resolved],
                             "problems": [p.public() for p in problems], "can_manage": True,
                             "can_manage_secrets": who.org_admin, "applies": "after_restart"},
                            headers=_NO_STORE)

    @router.get("/settings/schema")
    def schema(request: Request):
        hub_access.hub_admin(request, hub_dir)
        try:
            decls = hs.declarations(hub_dir)
        except HubFileError as exc:
            raise error(422, "validation_failed", str(exc)) from None
        return JSONResponse({"version": 1, "settings": [d.model_dump(exclude_none=True) for d in decls]},
                            headers=_NO_STORE)

    @router.post("/settings/validate")
    async def validate(request: Request):
        hub_access.hub_admin(request, hub_dir)
        document, _ = await _document(request)
        values = candidate(document)
        _, problems = problems_of(values)
        return JSONResponse({"valid": not problems, "problems": [p.public() for p in problems],
                             "changed_keys": changed(values), "base_revision": hs.revision(hub_dir)},
                            headers=_NO_STORE)

    @router.put("/settings")
    async def write(request: Request):
        who = hub_access.hub_admin(request, hub_dir)
        hub_access.same_origin(request)
        document, expected = await _document(request)
        values = candidate(document)
        try:
            current = hs.values_file(hub_dir)
        except HubFileError:
            current = None
        if values.secrets != (current.secrets if current else {}) and not who.org_admin:
            raise error(403, "permission_denied",
                        "Only organization administrators change which secret a setting uses.")
        _, problems = problems_of(values)
        if problems:
            raise error(422, "validation_failed", "The settings are not valid.",
                        fields=[p.public() for p in problems])
        if expected not in (None, "absent") and not isinstance(expected, int):
            raise error(422, "validation_failed", 'expected_revision is a number or "absent".')
        keys = changed(values)
        try:
            revision = hs.write(hub_dir, values.model_dump(), expected_revision=expected)
        except hs.Conflict as exc:
            raise error(409, "revision_conflict", "Someone changed these settings since you loaded them. "
                        "Reload and apply again.", current_revision=exc.current) from None
        from ..access import store_for
        from ..assertions import hub_key

        store_for(hub_dir).audit_event(who.email, "settings_apply", subject=",".join(keys)[:300],
                                       hub=hub_key(hub_dir), permission="settings.manage",
                                       surface="console", request_id=hub_access.request_id(request))
        return JSONResponse({"revision": revision, "changed_keys": keys, "restart_required": True},
                            headers=_NO_STORE)

    @router.post("/doctor")
    def doctor(request: Request):
        from .. import doctor as doctor_lib

        hub_access.hub_admin(request, hub_dir)
        hub_access.same_origin(request)
        wait = DOCTOR_EVERY - (time.monotonic() - last_run["at"])
        if wait > 0 or not doctor_lock.acquire(blocking=False):
            raise error(429, "rate_limited", "Checks ran a moment ago. Try again in a few seconds.",
                        retry_after=max(1, round(wait)))
        try:
            checks = doctor_lib._hub_setup_checks(hub_dir)
            last_run["at"] = time.monotonic()
        finally:
            doctor_lock.release()
        return JSONResponse({"ok": not any(c.status == "fail" for c in checks),
                             "checks": [asdict(c) for c in checks]}, headers=_NO_STORE)

    return router


def mount(app, hub_dir, **ctx) -> None:  # noqa: ARG001 - webapp passes runtime context
    app.include_router(build_router(Path(hub_dir)))
