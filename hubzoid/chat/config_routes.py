"""Chat configuration, preferences and theme routes (``/api/chat/...``).

  GET  /api/chat/config/schema      JSON Schema, defaults, scope and personal keys
  GET  /api/chat/config             active revision, document, sources, file state
  POST /api/chat/config/validate    normalised document and digest, changes nothing
  POST /api/chat/config/diff        changed keys against the active revision
  PUT  /api/chat/config             apply (expected_revision + Idempotency-Key)
  GET  /api/chat/config/history     applied revisions
  GET  /api/chat/bootstrap          public pre-sign-in defaults (no grants or names)
  GET  /api/chat/capabilities       this person's effective feature availability
  GET  /api/chat/preferences        this person's overrides and effective values
  PUT  /api/chat/preferences        replace them (expected_revision)
  GET  /api/chat/theme              the active resolved theme (public)
  POST /api/chat/theme/validate     validate a theme manifest, activates nothing

Reading the configuration needs ``chat_config.read``, applying it
``chat_config.manage`` (``hubzoid.control.authz``). Preferences belong to the
signed-in person only. The CLI (``hubzoid chat config ...``) calls the same
service, so file, CLI, API and Console share one schema and one apply.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..control import authz
from ..control import http as hz
from . import config as cfg
from . import theme as theme_mod

log = logging.getLogger("hubzoid.chat.config")


def _raise(exc: cfg.ConfigError):
    raise hz.error(exc.status, exc.code, exc.message, **exc.extra)


async def _reload_chat_runtime(request: Request) -> bool:
    """Rebuild this bridge's chat runtime from the hub's files and settings.
    False when this process has no chat or no reloader (the next start applies it)."""
    from .authoring_routes import _reload_runtime

    ctx = getattr(request.app.state, "chat", None)
    if ctx is None:
        return False
    try:
        return bool(await asyncio.wait_for(_reload_runtime(ctx), timeout=60))
    except Exception:  # noqa: BLE001 - the settings are applied; a restart applies the tool list
        log.warning("chat config: runtime rebuild failed", exc_info=True)
        return False


def _document(body: dict) -> dict:
    """``{"version": 1, "config": {...}}`` or ``{"config": {"version": 1, ...}}``."""
    config = body.get("config")
    if not isinstance(config, dict):
        raise hz.error(400, "invalid_request", "Send the settings as \"config\": {...}.")
    doc = dict(config)
    if "version" in body:
        doc.setdefault("version", body["version"])
    return doc


def active_theme(hub_dir: Path) -> tuple[str, dict]:
    """(theme revision, resolved theme) for this deployment: the active
    branding revision's tokens when one is active, else the built-in theme."""
    try:
        from ..ui import branding_store

        active = branding_store.active_theme(hub_dir)
    except Exception:  # noqa: BLE001 - branding unavailable: product theme
        log.warning("chat theme: active branding unavailable", exc_info=True)
        active = None
    if active is not None:
        return active
    return "builtin", theme_mod.resolve()


def mount(app: FastAPI, hub_dir: Path, **_ctx) -> None:
    hub_dir = Path(hub_dir)
    hz.install_request_ids(app)

    def svc() -> cfg.ChatConfigService:
        return cfg.service_for(hub_dir)

    async def call(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except cfg.ConfigError as exc:
            _raise(exc)

    @app.get("/api/chat/config/schema")
    async def config_schema(request: Request):
        await authz.principal(request, hub_dir)
        return cfg.schema()

    @app.get("/api/chat/config")
    async def config_get(request: Request):
        p = await authz.require_request(request, hub_dir, "chat_config.read")
        service = svc()
        active = await call(service.active)
        out = {"revision": active.revision, "digest": active.digest, "config": active.document,
               "applied_by": active.applied_by, "applied_at": active.applied_at,
               "file": await call(service.file_state),
               "capabilities": await call(service.capabilities, p)}
        if (request.query_params.get("sources") or "").lower() in ("1", "true", "yes"):
            out["sources"] = await call(service.sources)
        return out

    @app.post("/api/chat/config/validate")
    async def config_validate(request: Request):
        await authz.require_request(request, hub_dir, "chat_config.read")
        doc = _document(await hz.body(request))
        try:
            normalized = cfg.validate_document(doc)
        except cfg.ConfigError as exc:
            return JSONResponse({"valid": False, "error": exc.detail()},
                                status_code=200 if exc.status == 422 else exc.status)
        from ..control import canonical

        return {"valid": True, "normalized_config": normalized,
                "digest": canonical.digest(normalized), "warnings": []}

    @app.post("/api/chat/config/diff")
    async def config_diff(request: Request):
        await authz.require_request(request, hub_dir, "chat_config.read")
        body = await hz.body(request)
        doc = _document(body)
        try:
            normalized = cfg.validate_document(doc)
        except cfg.ConfigError as exc:
            _raise(exc)
        active = await call(svc().active)
        expected = body.get("expected_revision")
        stale = expected is not None and expected != "absent" and expected != active.revision
        return {"base_revision": active.revision, "stale": stale,
                "changes": cfg.diff(active.document, normalized)}

    @app.put("/api/chat/config")
    async def config_apply(request: Request):
        p = await authz.require_request(request, hub_dir, "chat_config.manage", mutation=True)
        body = await hz.body(request)
        doc = _document(body)
        expected = hz.expected_revision(body.get("expected_revision"))
        result = await call(svc().apply, doc, expected_revision=expected, actor=p.email,
                            idempotency_key=hz.idempotency_key(request))
        if any(c.get("pointer", "").startswith("/feedback/") for c in result.get("changed") or []):
            # The record_feedback tool is offered from the next turn: rebuild
            # this bridge's chat runtime (web and Slack replies). The tool also
            # checks the setting on every call, so turning it off holds at once.
            result["runtime_reloaded"] = await _reload_chat_runtime(request)
        return result

    @app.get("/api/chat/config/history")
    async def config_history(request: Request):
        await authz.require_request(request, hub_dir, "chat_config.read")
        return {"items": await call(svc().history)}

    @app.get("/api/chat/bootstrap")
    async def bootstrap():
        out = await call(svc().bootstrap)
        theme_revision, _ = await asyncio.to_thread(active_theme, hub_dir)
        out["theme_revision"] = theme_revision
        return out

    # -- devices (HUB-83) ------------------------------------------------------------
    from . import devices

    @app.get("/api/chat/shortcuts")
    async def shortcuts(request: Request):
        p = await authz.principal(request, hub_dir)
        personal = (await call(svc().get_preferences, p.id))["values"]
        return devices.effective_shortcuts(svc().active().document, personal)

    @app.post("/api/chat/shortcuts/validate")
    async def shortcuts_validate(request: Request):
        await authz.principal(request, hub_dir)
        body = await hz.body(request)
        dev = svc().active().document["devices"]["shortcuts"]
        try:
            clean = devices.check_shortcuts(body.get("shortcuts") or {}, single_key=dev["single_key"])
        except devices.Err as exc:
            return JSONResponse({"valid": False, "error": {"code": exc.code, "message": exc.message, **exc.extra}})
        return {"valid": True, "shortcuts": clean}

    @app.get("/api/chat/devices/doctor")
    async def devices_doctor(request: Request):
        await authz.principal(request, hub_dir)
        from .. import appmode

        return {"checks": devices.doctor(svc().active().document, public_url=appmode.public_url() or "")}

    @app.get("/api/chat/devices/support")
    async def devices_support():
        return devices.SUPPORT

    @app.get("/api/chat/connection")
    async def connection():
        import time as _t

        return JSONResponse({"ok": True, "server_time": _t.time()}, headers={"Cache-Control": "no-store"})

    @app.get("/api/chat/devices/settings")
    async def devices_settings():
        """Public, pre-sign-in: what the page needs before it can sign in."""
        dev = svc().active().document["devices"]
        return {k: dev[k] for k in ("install", "offline", "connection", "defaults", "allow_personal")}

    @app.get("/sw.js")
    async def service_worker():
        from fastapi.responses import Response

        return Response(devices.service_worker(svc().active().document), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/offline-shell")
    async def offline_shell():
        from fastapi.responses import HTMLResponse

        return HTMLResponse(devices.OFFLINE_SHELL, headers={"Cache-Control": "no-cache"})

    @app.get("/api/chat/capabilities")
    async def capabilities(request: Request):
        from . import features

        p = await authz.principal(request, hub_dir)
        out = await call(svc().capabilities, p)
        out["features"] = await asyncio.to_thread(features.for_person, hub_dir, p.email)

        def helpers_on() -> bool:
            from .feedback import FeedbackService

            try:
                return FeedbackService(hub_dir, None, "").helper_allowed("followups", p.id)
            except Exception:  # noqa: BLE001 - unknown: the chat simply doesn't ask
                return False

        # Whether the chat should ask for follow-up suggestions (deployment and person both on).
        out["features"]["helper_followups"] = await asyncio.to_thread(helpers_on)

        def feedback_on() -> bool:
            from .feedback import FeedbackService

            return FeedbackService(hub_dir, None, "").enabled()

        # Rating controls show only while the deployment collects feedback (the server refuses otherwise).
        out["features"]["feedback"] = await asyncio.to_thread(feedback_on)

        def web_on() -> bool:
            from .config import service_for

            try:
                web = service_for(hub_dir).active().document["retrieval"]["web"]
                return bool(web["enabled"] and web["search"] == "native")
            except Exception:  # noqa: BLE001
                return False

        # The web search switch shows only when the deployment allows it and the person holds the grant.
        out["features"]["web_search"] = bool(out["features"].get("chat_web_search")) and await asyncio.to_thread(web_on)
        return out

    @app.get("/api/chat/preferences")
    async def preferences_get(request: Request):
        p = await authz.principal(request, hub_dir)
        return await call(svc().get_preferences, p.id)

    @app.put("/api/chat/preferences")
    async def preferences_put(request: Request):
        authz.same_origin(request)
        p = await authz.principal(request, hub_dir)
        body = await hz.body(request)
        expected = hz.expected_revision(body.get("expected_revision"), allow_absent=False)
        return await call(svc().set_preferences, p.id, body.get("values"),
                          expected_revision=expected)

    @app.get("/api/chat/theme")
    async def theme_get():
        revision, resolved = await asyncio.to_thread(active_theme, hub_dir)
        out = theme_mod.public(resolved)
        out["revision"] = revision
        return JSONResponse(out, headers={"ETag": f'"{revision}"', "Cache-Control": "no-cache"})

    @app.post("/api/chat/theme/validate")
    async def theme_validate(request: Request):
        await authz.require_request(request, hub_dir, "branding.manage")
        body = await hz.body(request)
        manifest = body.get("manifest", body)
        try:
            resolved = theme_mod.resolve(manifest)
        except theme_mod.ThemeError as exc:
            return JSONResponse({"valid": False, "error": {"code": exc.code, "message": exc.message,
                                                           "pointer": exc.pointer, **exc.extra}})
        from ..control import canonical

        return {"valid": True, "digest": canonical.digest(resolved), "resolved": theme_mod.public(resolved),
                "diagnostics": []}

    log.info("chat config: mounted")
