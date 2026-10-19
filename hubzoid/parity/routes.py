"""Parity ledger API (``/api/parity/...``).

  POST /api/parity/validations                 validate a bundle (or the deployment's parity.yaml)
  POST /api/parity/candidates                  make a candidate (201, or a valid blocked candidate)
  GET  /api/parity/candidates                  candidates, newest first
  GET  /api/parity/candidates/{id}             one candidate with its reviews and freeze
  GET  /api/parity/candidates/{id}/coverage    its full report
  GET  /api/parity/candidates/{id}/diff?base=  obligations added, removed and changed
  POST /api/parity/candidates/{id}/reviews     approve or reject the exact digest
  POST /api/parity/candidates/{id}/freeze      seal an approved, unblocked candidate

The server never reads a path a caller names: it takes the bundle in the
body, or reads the deployment's own ``parity.yaml``. Reading needs
``parity.read``, candidates ``parity.write``, reviews and freezes
``parity.review`` (checked again at freeze).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Request

from ..control import authz
from ..control import http as hz
from . import store
from .validate import Bundle, ParityError, check, load


def _bundle(body: dict, hub_dir: Path) -> Bundle:
    if body.get("from_source"):
        svc = store.service_for(hub_dir)
        return load(svc.root, "parity.yaml")
    raw = body.get("bundle")
    if not isinstance(raw, dict):
        raise ParityError(400, "invalid_request", "Send the bundle, or from_source: true for parity.yaml.")
    try:
        return Bundle(raw["manifest"], raw["ledger"], raw["sources"], raw["providers"], raw.get("deployments") or {})
    except (KeyError, TypeError) as exc:
        raise ParityError(422, "invalid_schema", f"The bundle is missing {exc}.") from exc


def mount(app: FastAPI, hub_dir: Path, **_ctx) -> None:
    hub_dir = Path(hub_dir)

    async def call(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ParityError as exc:
            raise hz.error(exc.status, exc.code, exc.message, **exc.extra)

    @app.post("/api/parity/validations")
    async def validations(request: Request):
        await authz.require_request(request, hub_dir, "parity.read")
        body = await hz.body(request)
        bundle = await call(_bundle, body, hub_dir)
        return await call(check, bundle)

    @app.post("/api/parity/candidates", status_code=201)
    async def create(request: Request):
        p = await authz.require_request(request, hub_dir, "parity.write", mutation=True)
        body = await hz.body(request)
        bundle = await call(_bundle, body, hub_dir)
        return await call(store.service_for(hub_dir).create, bundle, actor=p.email, base_id=body.get("base_id"),
                          idempotency_key=hz.idempotency_key(request))

    @app.get("/api/parity/candidates")
    async def list_(request: Request):
        await authz.require_request(request, hub_dir, "parity.read")
        return {"items": await call(store.service_for(hub_dir).list)}

    @app.get("/api/parity/candidates/{cid}")
    async def get(cid: str, request: Request):
        await authz.require_request(request, hub_dir, "parity.read")
        return await call(store.service_for(hub_dir).get, cid)

    @app.get("/api/parity/candidates/{cid}/coverage")
    async def coverage(cid: str, request: Request):
        await authz.require_request(request, hub_dir, "parity.read")
        bundle = await call(store.service_for(hub_dir).bundle, cid)
        return await call(check, bundle)

    @app.get("/api/parity/candidates/{cid}/diff")
    async def diff(cid: str, request: Request):
        await authz.require_request(request, hub_dir, "parity.read")
        base = request.query_params.get("base") or ""
        return await call(store.service_for(hub_dir).diff, base, cid)

    @app.post("/api/parity/candidates/{cid}/reviews", status_code=201)
    async def review(cid: str, request: Request):
        p = await authz.require_request(request, hub_dir, "parity.review", mutation=True)
        body = await hz.body(request)
        expected = hz.expected_revision(body.get("expected_revision"), allow_absent=False)
        return await call(store.service_for(hub_dir).review, cid, digest=str(body.get("digest") or ""),
                          expected_revision=expected, decision=str(body.get("decision") or ""),
                          reason=body.get("reason"), actor=p.email, idempotency_key=hz.idempotency_key(request))

    @app.post("/api/parity/candidates/{cid}/freeze", status_code=201)
    async def freeze(cid: str, request: Request):
        p = await authz.require_request(request, hub_dir, "parity.review", mutation=True)
        body = await hz.body(request)
        expected = hz.expected_revision(body.get("expected_revision"), allow_absent=False)
        return await call(store.service_for(hub_dir).freeze, cid, digest=str(body.get("digest") or ""),
                          review_id=str(body.get("review_id") or ""), expected_revision=expected, actor=p.email,
                          idempotency_key=hz.idempotency_key(request))
