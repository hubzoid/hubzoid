"""Branding routes (``/api/branding/...``, ``/branding/assets/...``).

  GET   /api/branding/source                         where the brand is authored, legacy files
  GET   /api/branding/drafts                         drafts, newest first
  POST  /api/branding/drafts                         create (configuration, theme, files)
  POST  /api/branding/drafts/from-source             create from the deployment's branding.yaml
  GET   /api/branding/drafts/{id}                    one draft
  GET   /api/branding/active                         the active revision's authored files
  PATCH /api/branding/drafts/{id}                    replace configuration and theme
  PUT   /api/branding/drafts/{id}/assets?path=...    upload one referenced file (raw bytes)
  POST  /api/branding/drafts/{id}/validate           re-check against base and validator
  GET   /api/branding/drafts/{id}/diff               changes against the active brand
  GET   /api/branding/drafts/{id}/preview            the resolved candidate (private)
  GET   /api/branding/drafts/{id}/assets/{digest}    a candidate file (private, no-store)
  POST  /api/branding/drafts/{id}/reviews            approve or reject an exact digest
  POST  /api/branding/drafts/{id}/activate           activate a reviewed draft
  GET   /api/branding/history                        activations
  GET   /api/branding/revisions/{id}/export          the revision as a zip of files
  POST  /api/branding/rollbacks                      a new draft from a retained revision
  GET   /api/branding/me                             my terms and announcement state
  POST  /api/branding/me/legal-acceptances           accept the current terms
  POST  /api/branding/me/announcement-dismissals     dismiss an announcement
  GET   /branding/assets/{deployment}/{hex}/{name}   activated assets (public, immutable)

``GET /api/branding`` (public manifest) stays in ``webapp_gateway``.
Reading drafts needs ``branding.read``, changing them ``branding.manage``,
reviewing and activating ``branding.activate``.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response

from ..control import authz
from ..control import http as hz
from . import branding_store as store
from .branding_models import BrandError

log = logging.getLogger("hubzoid.ui.branding")

MAX_UPLOAD = 5 * 1024 * 1024


def _files(raw) -> dict[str, bytes]:
    """``{"branding/logo.svg": "<base64>"}`` from a JSON body."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise hz.error(400, "invalid_request", "files is an object of path to base64 bytes.")
    out = {}
    for path, value in raw.items():
        try:
            out[str(path)] = base64.b64decode(value, validate=True)
        except (binascii.Error, TypeError) as exc:
            raise hz.error(400, "invalid_request", f"files[{path!r}] is not base64.") from exc
    return out


def mount(app: FastAPI, hub_dir: Path, **_ctx) -> None:
    hub_dir = Path(hub_dir)
    hz.install_request_ids(app)

    def svc() -> store.BrandingService:
        return store.service_for(hub_dir)

    async def call(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except BrandError as exc:
            raise hz.error(exc.status, exc.code, exc.message, **exc.extra)

    @app.get("/api/branding/source")
    async def source(request: Request):
        await authz.require_request(request, hub_dir, "branding.read")
        service = svc()
        root = service.root
        configuration, _files_found, report = await call(service.legacy_import)
        return {"deployment_id": service.deployment_id, "folder": ".",
                "file": "branding.yaml", "file_exists": (root / "branding.yaml").is_file(),
                "theme_file_exists": (root / "branding" / "theme.json").is_file(),
                "active": await call(service.active_state), "legacy": report}

    @app.get("/api/branding/drafts")
    async def list_drafts(request: Request):
        await authz.require_request(request, hub_dir, "branding.read")
        return {"items": await call(svc().list_drafts)}

    @app.post("/api/branding/drafts", status_code=201)
    async def create_draft(request: Request):
        p = await authz.require_request(request, hub_dir, "branding.manage", mutation=True)
        body = await hz.body(request)
        files = _files(body.get("files"))
        if body.get("keep_active_files"):
            # Files the new manifest still references come from the active brand.
            kept = await call(svc().active_files)
            files = {**kept, **files}
        draft = await call(svc().create_draft, body.get("configuration"), body.get("theme"),
                           files, actor=p.email, idempotency_key=hz.idempotency_key(request))
        return await call(svc().view, draft)

    @app.post("/api/branding/drafts/from-source", status_code=201)
    async def draft_from_source(request: Request):
        """A draft from the deployment folder's branding.yaml, theme.json and
        files: what `hubzoid branding draft create` does, from the Console."""
        p = await authz.require_request(request, hub_dir, "branding.manage", mutation=True)
        service = svc()
        configuration, theme, files = await call(service.read_source)
        draft = await call(service.create_draft, configuration, theme, files, actor=p.email,
                           idempotency_key=hz.idempotency_key(request), source="console")
        return draft.public()

    @app.get("/api/branding/drafts/{draft_id}")
    async def get_draft(draft_id: str, request: Request):
        await authz.require_request(request, hub_dir, "branding.read")
        return await call(svc().view, await call(svc().get_draft, draft_id))

    @app.get("/api/branding/active")
    async def active_source(request: Request):
        await authz.require_request(request, hub_dir, "branding.read")
        return {"active": await call(svc().active_source)}

    @app.patch("/api/branding/drafts/{draft_id}")
    async def patch_draft(draft_id: str, request: Request):
        p = await authz.require_request(request, hub_dir, "branding.manage", mutation=True)
        body = await hz.body(request)
        expected = hz.expected_revision(body.get("expected_revision"), allow_absent=False)
        draft = await call(svc().update_draft, draft_id, body.get("configuration"), body.get("theme"),
                           _files(body.get("files")), expected_revision=expected, actor=p.email)
        return draft.public()

    @app.put("/api/branding/drafts/{draft_id}/assets")
    async def put_asset(draft_id: str, request: Request):
        p = await authz.require_request(request, hub_dir, "branding.manage", mutation=True)
        path = request.query_params.get("path") or ""
        data = await request.body()
        if len(data) > MAX_UPLOAD:
            raise hz.error(413, "asset_too_large", "Brand files are at most 5 MiB each.")
        return (await call(svc().put_asset, draft_id, path, data, actor=p.email)).public()

    @app.post("/api/branding/drafts/{draft_id}/validate")
    async def validate(draft_id: str, request: Request):
        await authz.require_request(request, hub_dir, "branding.manage", mutation=True)
        return (await call(svc().revalidate, draft_id)).public()

    @app.get("/api/branding/drafts/{draft_id}/diff")
    async def diff(draft_id: str, request: Request):
        await authz.require_request(request, hub_dir, "branding.read")
        return await call(svc().diff, draft_id)

    @app.get("/api/branding/drafts/{draft_id}/preview")
    async def preview(draft_id: str, request: Request):
        await authz.require_request(request, hub_dir, "branding.manage")
        out = await call(svc().preview, draft_id)
        return Response(content=json.dumps(out), media_type="application/json",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/branding/drafts/{draft_id}/assets/{digest}")
    async def draft_asset(draft_id: str, digest: str, request: Request):
        await authz.require_request(request, hub_dir, "branding.manage")
        got = await call(svc().draft_asset, draft_id, digest)
        if got is None:
            raise hz.error(404, "not_found", "That file is not part of this draft.")
        data, media = got
        return Response(content=data, media_type=media,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    @app.post("/api/branding/drafts/{draft_id}/reviews", status_code=201)
    async def review(draft_id: str, request: Request):
        p = await authz.require_request(request, hub_dir, "branding.activate", mutation=True)
        body = await hz.body(request)
        expected = hz.expected_revision(body.get("expected_revision"), allow_absent=False)
        return await call(svc().review, draft_id, digest=str(body.get("digest") or ""),
                          expected_revision=expected, decision=str(body.get("decision") or ""),
                          reason=body.get("reason"), actor=p.email,
                          idempotency_key=hz.idempotency_key(request))

    @app.post("/api/branding/drafts/{draft_id}/activate")
    async def activate(draft_id: str, request: Request):
        # Permission is checked again here, at activation, not only at review.
        p = await authz.require_request(request, hub_dir, "branding.activate", mutation=True)
        body = await hz.body(request)
        expected = hz.expected_revision(body.get("expected_revision"), allow_absent=False)
        return await call(svc().activate, draft_id, review_id=str(body.get("review_id") or ""),
                          expected_generation=expected, actor=p.email,
                          idempotency_key=hz.idempotency_key(request))

    @app.get("/api/branding/history")
    async def history(request: Request):
        await authz.require_request(request, hub_dir, "branding.read")
        return {"items": await call(svc().history), "active": await call(svc().active_state)}

    @app.get("/api/branding/revisions/{revision_id}/export")
    async def export(revision_id: str, request: Request):
        await authz.require_request(request, hub_dir, "branding.read")
        data = await call(svc().export, revision_id)
        return Response(content=data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="brand-{revision_id[:8]}.zip"',
                                 "Cache-Control": "no-store"})

    @app.post("/api/branding/rollbacks", status_code=201)
    async def rollback(request: Request):
        p = await authz.require_request(request, hub_dir, "branding.manage", mutation=True)
        body = await hz.body(request)
        draft = await call(svc().rollback, str(body.get("target_revision") or ""), actor=p.email,
                           idempotency_key=hz.idempotency_key(request))
        return draft.public()

    @app.get("/api/branding/me")
    async def me(request: Request):
        p = await authz.principal(request, hub_dir)
        return await call(svc().me, p.id)

    @app.post("/api/branding/me/legal-acceptances")
    async def accept(request: Request):
        authz.same_origin(request)
        p = await authz.principal(request, hub_dir)
        body = await hz.body(request)
        return await call(svc().accept_terms, p.id, version=str(body.get("version") or ""),
                          policy_hash=str(body.get("policy_hash") or ""))

    @app.post("/api/branding/me/announcement-dismissals")
    async def dismiss(request: Request):
        authz.same_origin(request)
        p = await authz.principal(request, hub_dir)
        body = await hz.body(request)
        return await call(svc().dismiss, p.id, str(body.get("announcement_id") or "")[:80])

    @app.get("/branding/assets/{deployment}/{hexdigest}/{name}")
    async def public_asset(deployment: str, hexdigest: str, name: str):
        service = svc()
        if deployment != service.public_id() or len(hexdigest) != 64:
            raise hz.error(404, "not_found", "Not found.")
        got = await asyncio.to_thread(service.public_asset, hexdigest)
        if got is None:
            raise hz.error(404, "not_found", "Not found.")
        data, media = got
        return Response(content=data, media_type=media, headers={
            "Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
