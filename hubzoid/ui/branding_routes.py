"""Branding routes (``/api/branding/...``, ``/branding/assets/...``).

  GET   /api/branding/source                         where the brand comes from (read-only)
  GET   /api/branding/active                         the brand's files, as read from the folder
  GET   /api/branding/me                             my terms and announcement state
  POST  /api/branding/me/legal-acceptances           accept the current terms
  POST  /api/branding/me/announcement-dismissals     dismiss an announcement
  GET   /branding/assets/{deployment}/{hex}/{name}   the brand's files (public, immutable)
  GET   /manifest.webmanifest                        install metadata of the brand (public)

The brand lives in the deployment folder (``hubzoid.ui.branding_store``):
nothing here changes it. ``GET /api/branding`` (public manifest) stays in
``webapp_gateway``. Reading the source needs ``branding.read``.
"""
from __future__ import annotations

import asyncio
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
        state = await call(service.active_state)
        return {"deployment_id": service.deployment_id, "folder": str(root),
                "file": state.get("source") or "hubzoid.yaml (brand)",
                "file_exists": state.get("revision") is not None,
                "theme_file_exists": (root / "branding" / "theme.json").is_file(),
                "active": state, "legacy": report, "read_only": True,
                "how_to_change": "Edit the brand in hubzoid.yaml (brand:) and its files in branding/, or ask "
                                 "Claude Code or Codex to. Every agent picks it up within a few seconds."}

    @app.get("/api/branding/active")
    async def active_source(request: Request):
        await authz.require_request(request, hub_dir, "branding.read")
        return {"active": await call(svc().active_source)}

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

    @app.get("/manifest.webmanifest")
    async def web_manifest():
        """Install metadata from the active brand (public, like the sign-in page)."""
        def build() -> dict:
            from ..chat import theme as theme_mod

            manifest = svc().active_manifest()
            got = svc().active_theme()
            from ..chat import devices  # noqa: F401 - registers the devices namespace
            from ..chat.config import service_for as config_service

            palettes = (got[1] if got else theme_mod.resolve())["palettes"]
            dev = config_service(hub_dir).active().document["devices"]
            light = palettes["dark"] if config_service(hub_dir).active().document["presentation"]["appearance"][
                "default_mode"] in ("dark", "oled") else palettes["light"]
            identity = (manifest or {}).get("identity") or {}
            assets = (manifest or {}).get("assets") or {}
            icons = [{"src": assets[k], "sizes": size, "type": "image/png"}
                     for k, size in (("icon_192", "192x192"), ("icon_512", "512x512")) if assets.get(k)]
            out = {"name": identity.get("name") or "Hubzoid", "short_name": identity.get("short_name") or "Hubzoid",
                   "description": identity.get("description") or "", "start_url": dev["install"]["start_path"],
                   "scope": "/", "display": dev["install"]["display"], "theme_color": light["canvas"],
                   "background_color": light["canvas"], "icons": icons}
            if dev["install"]["share_target"] and dev["offline"]["shell_cache"]:
                out["share_target"] = {"action": "/", "method": "GET",
                                       "params": {"title": "share_title", "text": "share_text", "url": "share_url"}}
            return out

        from ..chat import devices  # noqa: F401
        from ..chat.config import service_for as config_service

        if not config_service(hub_dir).active().document["devices"]["install"]["enabled"]:
            raise hz.error(404, "not_found", "Installing this app is turned off for this deployment.")
        return Response(content=json.dumps(await asyncio.to_thread(build)),
                        media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})

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
