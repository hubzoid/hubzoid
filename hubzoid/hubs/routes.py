"""HTTP for a hub's declared settings and its own checks (mounted by ``hubzoid.webapp``).

    GET  /api/hubs/{hub_id}/settings            every declared setting, its value or
                                                reference, source and status, and the revision
    GET  /api/hubs/{hub_id}/settings/schema     the declarations
    POST /api/hubs/{hub_id}/settings/validate   {version: 1, values, secrets} -> every problem
    PUT  /api/hubs/{hub_id}/settings            the same body with expected_revision -> written
    POST /api/hubs/{hub_id}/doctor              run the hub's own checks now
    GET  /api/hubs/{hub_id}/get-started         the Get started cards, checked in the last
                                                30 seconds, else now (?refresh=1: 10 seconds)

This hub's administrators read, change values and run checks. Anyone else
gets 404, as if the route did not exist. Changing a secret reference needs an
organization administrator, because a reference decides which stored secret
the hub's code receives. A secret value is never accepted or returned here. A
saved change applies after the hub restarts.

The hub's checks run once at a time: a request for the Get started cards while
they are being checked waits for that run, and doctor answers 429 while any
checks run. Errors are ``{"detail": {"code", "message", ...}}``. Writes, and
the Get started cards (they run hub code), need the site's own origin.
"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future
from dataclasses import asdict
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import hub_access, hub_routes
from ..chat.common import error
from . import settings as hs
from .yamlfile import HubFileError

_NO_STORE = {"Cache-Control": "no-store"}
DOCTOR_EVERY = 10.0  # seconds between two runs of a hub's checks
GET_STARTED_EVERY = 30.0  # seconds the Get started cards are kept
GET_STARTED_AGAIN = 10.0  # seconds after a run in which Check again (?refresh=1) gets that run's cards


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
    doctor_lock = threading.Lock()  # held while doctor's or the Get started checks run
    last_run = {"at": 0.0}
    cards_lock = threading.Lock()  # guards `cards` only, never held while checks run
    cards: dict = {"at": 0.0, "body": None, "run": None}  # run: a Future while the cards are checked

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
        errors = [p for p in problems if p.blocking]
        return JSONResponse({"valid": not errors, "problems": [p.public() for p in errors],
                             "still_needed": [p.public() for p in problems if not p.blocking],
                             "changed_keys": changed(values), "base_revision": hs.revision(hub_dir)},
                            headers=_NO_STORE)

    @router.put("/settings")
    async def write(request: Request):
        who = hub_access.hub_admin(request, hub_dir)
        hub_access.same_origin(request)
        from .. import editing

        editing.check(hub_dir, "this agent's settings (settings.yaml)")
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
        errors = [p for p in problems if p.blocking]
        if errors:
            raise error(422, "validation_failed", "The settings are not valid.",
                        fields=[p.public() for p in errors])
        if expected not in (None, "absent") and not isinstance(expected, int):
            raise error(422, "validation_failed", 'expected_revision is a number or "absent".')
        keys = changed(values)
        try:
            revision = hs.write(hub_dir, values.model_dump(), expected_revision=expected)
        except HubFileError as exc:
            raise error(413, "validation_failed", str(exc)) from None
        except hs.ReadOnly as exc:
            raise error(409, "hub_read_only", str(exc)) from None
        except hs.Conflict as exc:
            raise error(409, "revision_conflict", "Someone changed these settings since you loaded them. "
                        "Reload and apply again.", current_revision=exc.current) from None
        from ..access import store_for
        from ..assertions import hub_key

        store_for(hub_dir).audit_event(who.email, "settings_apply", subject=",".join(keys)[:300],
                                       hub=hub_key(hub_dir), permission="settings.manage",
                                       surface="console", request_id=hub_access.request_id(request))
        return JSONResponse({"revision": revision, "changed_keys": keys, "restart_required": True,
                             "still_needed": [p.public() for p in problems if not p.blocking]},
                            headers=_NO_STORE)

    @router.post("/doctor")
    def doctor(request: Request):
        from .. import doctor as doctor_lib

        hub_access.hub_admin(request, hub_dir)
        hub_access.same_origin(request)
        wait = DOCTOR_EVERY - (time.monotonic() - last_run["at"])
        if wait > 0:
            raise error(429, "rate_limited", "Checks ran a moment ago. Try again in a few seconds.",
                        retry_after=max(1, round(wait)))
        if not doctor_lock.acquire(blocking=False):  # doctor's or the Get started checks are running
            raise error(429, "rate_limited", "Checks are running now. Try again shortly.",
                        retry_after=round(DOCTOR_EVERY))
        try:
            checks = doctor_lib._hub_setup_checks(hub_dir)
            last_run["at"] = time.monotonic()
        finally:
            doctor_lock.release()
        return JSONResponse({"ok": not any(c.status == "fail" for c in checks),
                             "checks": [asdict(c) for c in checks]}, headers=_NO_STORE)

    def check_cards(run: Future, fresh: bool) -> None:
        """One run of the Get started checks, never while doctor's run. Every
        request waiting on ``run`` gets its answer. An error is not kept."""
        from . import get_started as get_started_lib

        try:
            with doctor_lock:
                body = get_started_lib.build(hub_dir, fresh=fresh)
        except BaseException as exc:
            with cards_lock:
                cards["run"] = None
            run.set_exception(exc)
            raise
        with cards_lock:
            cards.update(at=time.monotonic(), body=body if body and "error" not in body else None, run=None)
        run.set_result(body)

    @router.get("/get-started")
    async def get_started(request: Request, refresh: str = ""):
        from . import get_started as get_started_lib

        hub_access.hub_admin(request, hub_dir)
        hub_access.same_origin_read(request)
        if not get_started_lib.present(hub_dir):
            raise error(404, "not_found", "This agent has no Get started cards.")
        again = refresh in ("1", "true")
        keep = GET_STARTED_AGAIN if again else GET_STARTED_EVERY
        with cards_lock:
            run, body = cards["run"], cards["body"]
            lead = run is None and (body is None or time.monotonic() - cards["at"] >= keep)
            if lead:
                run = cards["run"] = Future()
        if lead:  # one worker runs the checks; the others wait here without holding one
            await run_in_threadpool(check_cards, run, again)
        if run is not None:
            body = await asyncio.wrap_future(run)
        if body is None:
            raise error(404, "not_found", "This agent has no Get started cards.")
        return JSONResponse(body, headers=_NO_STORE)

    return router


def mount(app, hub_dir, **ctx) -> None:  # noqa: ARG001 - webapp passes runtime context
    app.include_router(build_router(Path(hub_dir)))
