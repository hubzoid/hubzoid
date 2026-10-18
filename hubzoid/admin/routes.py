"""Console administration API (``/api/admin/...``).

  GET  /api/admin/capabilities                       what I may do here
  GET  /api/admin/overview                           cards: value, as_of, availability (unknown is null)
  GET  /api/admin/config                             admin.yaml values, defaults, provenance, revision
  POST /api/admin/config/validate                    normalised values or the field error
  POST /api/admin/config/plans                       a plan: digest, diff, expiry
  GET  /api/admin/config/plans                       recent plans
  POST /api/admin/config/plans/{id}/apply            apply by id, digest and expected revision
  GET  /api/admin/config/export?format=yaml|bundle   the active document, or secret-free metadata
  POST /api/admin/config/import/validate             check an export before planning it
  GET  /api/admin/domains[/{domain}]                 the settings registry
  GET  /api/admin/diagnostics/checks                 check catalog
  POST /api/admin/diagnostics/reports                start a report (202)
  GET  /api/admin/diagnostics/reports[/{id}]         reports, one report
  POST /api/admin/diagnostics/reports/{id}/cancel    cancel
  POST /api/admin/diagnostics/reports/{id}/bundles   a support bundle
  GET  /api/admin/diagnostics/bundles/{id}[/download] · DELETE /api/admin/diagnostics/bundles/{id}

Entry needs ``admin.access``; each operation its own capability, checked
again on every poll and download (``hubzoid.control.authz``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ..control import authz
from ..control import http as hz
from . import config as admin_cfg
from . import diagnostics as diag
from .config import AdminError

log = logging.getLogger("hubzoid.admin")


def overview(hub_dir: Path, principal, days: int = 7) -> dict:
    """Deployment cards. A figure Hubzoid can't know is null, never zero."""
    import sqlalchemy as sa

    from .. import db

    now = time.time()
    since = now - days * 86400
    cards: dict = {}
    hubs = None if principal.org_admin else list(principal.hubs)
    try:
        engine = db.operational_engine(hub_dir)
        with engine.connect() as conn:
            q = "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd), " \
                "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END), " \
                "SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) FROM hz_usage WHERE ts >= :s"
            params: dict = {"s": since}
            if hubs is not None:
                if not hubs:
                    raise LookupError
                q += " AND hub IN (" + ",".join(f":h{i}" for i in range(len(hubs))) + ")"
                params.update({f"h{i}": h for i, h in enumerate(hubs)})
            turns, tin, tout, cost, unknown, errors = conn.execute(sa.text(q), params).first()
        cards["turns"] = {"value": int(turns or 0), "availability": "available"}
        cards["tokens"] = {"value": int((tin or 0) + (tout or 0)), "availability": "available"}
        cards["errors"] = {"value": int(errors or 0), "availability": "available"}
        cards["cost_usd"] = {"value": None if (turns and unknown == turns) else round(cost or 0, 4),
                             "availability": "partial" if unknown else "available",
                             "unpriced_turns": int(unknown or 0), "note": "Estimated. Unpriced turns are not zero."}
    except LookupError:
        cards["turns"] = cards["tokens"] = cards["errors"] = cards["cost_usd"] = {"value": None,
                                                                                    "availability": "none"}
    except Exception:  # noqa: BLE001 - the card says unknown, the page still loads
        log.exception("admin overview: usage unavailable")
        for k in ("turns", "tokens", "errors", "cost_usd"):
            cards[k] = {"value": None, "availability": "unavailable"}
    try:
        last = diag.service_for(hub_dir).list(hub_ids=None if principal.org_admin else list(principal.hubs), limit=1)
        item = last["items"][0] if last["items"] else None
        cards["diagnostics"] = {"value": item and item["status"], "report_id": item and item["report_id"],
                                "availability": "available", "link": "#/diagnostics"}
    except Exception:  # noqa: BLE001
        cards["diagnostics"] = {"value": None, "availability": "unavailable"}
    for c in cards.values():
        c["as_of"] = now
    return {"interval_days": days, "cards": cards}


def mount(app: FastAPI, hub_dir: Path, **_ctx) -> None:
    hub_dir = Path(hub_dir)
    hz.install_request_ids(app)
    try:
        diag.service_for(hub_dir).recover()
    except Exception:  # noqa: BLE001 - never block the bridge start
        log.warning("diagnostics: recovery check failed", exc_info=True)

    async def call(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except AdminError as exc:
            raise hz.error(exc.status, exc.code, exc.message, **exc.extra)

    async def entry(request: Request):
        return await authz.require_request(request, hub_dir, "admin.access")

    def scope_hub(p, hub: str | None, capability: str):
        """A hub-scoped operation needs the capability there; a
        deployment-wide one needs it org-wide."""
        if hub:
            authz.require(p, capability, hub)
        elif not p.org_admin:
            raise authz.denied(capability)

    @app.get("/api/admin/capabilities")
    async def capabilities(request: Request):
        p = await entry(request)
        return {"principal": {"email": p.email, "org_admin": p.org_admin, "hubs": list(p.hubs)},
                "capabilities": p.capabilities(), "hub_capabilities": {h: p.capabilities(h) for h in p.hubs},
                "domains": await call(admin_cfg.domains, hub_dir, p), "diagnostics": diag.catalog()}

    @app.get("/api/admin/overview")
    async def get_overview(request: Request):
        p = await entry(request)
        try:
            days = max(1, min(int(request.query_params.get("days") or 7), 90))
        except ValueError:
            raise hz.error(422, "validation_failed", "days is a number from 1 to 90.")
        return await asyncio.to_thread(overview, hub_dir, p, days)

    # -- admin.yaml ---------------------------------------------------------------
    @app.get("/api/admin/config")
    async def config_get(request: Request):
        p = await entry(request)
        authz.require(p, "admin.config.read")
        return await call(admin_cfg.service_for(hub_dir).show)

    @app.post("/api/admin/config/validate")
    async def config_validate(request: Request):
        p = await entry(request)
        authz.require(p, "admin.config.read")
        body = await hz.body(request)
        try:
            values = admin_cfg.validate(body.get("values"))
        except AdminError as exc:
            return {"valid": False, "error": {"code": exc.code, "message": exc.message, **exc.extra}}
        return {"valid": True, "values": values}

    @app.post("/api/admin/config/plans", status_code=201)
    async def config_plan(request: Request):
        p = await authz.require_request(request, hub_dir, "admin.config.read", mutation=True)
        body = await hz.body(request)
        if (body.get("domain") or "admin") != "admin":
            raise hz.error(422, "validation_failed",
                           "Plan other domains with their owner: chat through /api/chat/config, branding "
                           "through a reviewed draft.")
        expected = hz.expected_revision(body.get("expected_revision"), allow_absent=False)
        return await call(admin_cfg.service_for(hub_dir).plan, body.get("values"), expected_revision=expected,
                          actor=p.email, idempotency_key=hz.idempotency_key(request))

    @app.get("/api/admin/config/plans")
    async def config_plans(request: Request):
        p = await entry(request)
        authz.require(p, "admin.config.read")
        return {"items": await call(admin_cfg.service_for(hub_dir).list_plans)}

    @app.post("/api/admin/config/plans/{plan_id}/apply")
    async def config_apply(plan_id: str, request: Request):
        p = await authz.require_request(request, hub_dir, "admin.config.write", mutation=True)
        body = await hz.body(request)
        expected = hz.expected_revision(body.get("expected_revision"), allow_absent=False)
        return await call(admin_cfg.service_for(hub_dir).apply, plan_id, digest=str(body.get("digest") or ""),
                          expected_revision=expected, actor=p.email,
                          idempotency_key=hz.idempotency_key(request))

    @app.get("/api/admin/config/export")
    async def config_export(request: Request):
        p = await entry(request)
        authz.require(p, "admin.config.read")
        fmt = request.query_params.get("format") or "yaml"
        svc = admin_cfg.service_for(hub_dir)
        if fmt == "yaml":
            text = await call(svc.export_yaml)
            return Response(text, media_type="application/yaml",
                            headers={"Content-Disposition": 'attachment; filename="admin.yaml"'})
        if fmt == "bundle":
            shown = await call(svc.show)
            return {"format": "bundle", "domain": "admin", "scope": {"deployment_id": svc.deployment_id},
                    "version": 1, "source_revision": shown["revision"], "values": shown["values"],
                    "omitted_secret_fields": [], "restart_required": False}
        raise hz.error(422, "validation_failed", "format is yaml or bundle.")

    @app.post("/api/admin/config/import/validate")
    async def config_import_validate(request: Request):
        p = await entry(request)
        authz.require(p, "admin.config.read")
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            raise hz.error(413, "too_large", "Imports are at most 1 MiB.")
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise hz.error(400, "invalid_request", "The export is not valid JSON.")
        if data.get("domain") != "admin" or data.get("format") != "bundle":
            raise hz.error(422, "validation_failed", "This import takes an admin bundle export.")
        unsupported = sorted(set(data) - {"format", "domain", "scope", "version", "source_revision", "values",
                                          "omitted_secret_fields", "restart_required"})
        try:
            values = admin_cfg.validate(data.get("values"))
        except AdminError as exc:
            return {"valid": False, "error": {"code": exc.code, "message": exc.message, **exc.extra}}
        shown = await call(admin_cfg.service_for(hub_dir).show)
        return {"valid": True, "values": values, "unsupported_fields": unsupported,
                "diff": admin_cfg.diff(shown["values"], values), "base_revision": shown["revision"],
                "next": "POST /api/admin/config/plans with these values"}

    @app.get("/api/admin/domains")
    async def domains(request: Request):
        p = await entry(request)
        return {"items": await call(admin_cfg.domains, hub_dir, p)}

    @app.get("/api/admin/domains/{domain}")
    async def domain(domain: str, request: Request):
        p = await entry(request)
        found = next((d for d in await call(admin_cfg.domains, hub_dir, p) if d["domain"] == domain), None)
        if found is None or not found.get("can_read", False):
            raise hz.error(404, "not_found", "No such settings domain here.")
        return found

    # -- diagnostics ----------------------------------------------------------------
    @app.get("/api/admin/diagnostics/checks")
    async def checks(request: Request):
        await entry(request)
        return {"items": diag.catalog()}

    @app.post("/api/admin/diagnostics/reports", status_code=202)
    async def start_report(request: Request):
        p = await authz.require_request(request, hub_dir, "admin.access", mutation=True)
        body = await hz.body(request)
        scope = body.get("scope") or {}
        hub = scope.get("hub_id") if isinstance(scope, dict) else None
        scope_hub(p, hub, "admin.diagnostics.run")
        profile = body.get("profile") or "passive"
        if profile == "active":
            scope_hub(p, hub, "admin.diagnostics.network")
        out = await call(diag.service_for(hub_dir).start, actor=p.email, profile=profile,
                         check_ids=body.get("check_ids"), hub_id=hub,
                         consent_network=bool(body.get("consent_network")),
                         idempotency_key=hz.idempotency_key(request),
                         previous_report_id=body.get("previous_report_id"))
        return JSONResponse(out, status_code=202, headers={"Location": out.get("location", "")})

    def visible_hubs(p) -> list[str] | None:
        return None if p.org_admin else list(p.hubs)

    async def owned_report(p, report_id: str) -> None:
        hub = await call(diag.service_for(hub_dir).hub_of, report_id)
        if hub:
            if not p.can("admin.diagnostics.read", hub):
                raise hz.error(404, "not_found", "That report was not found.")
        elif not p.org_admin:
            raise hz.error(404, "not_found", "That report was not found.")

    @app.get("/api/admin/diagnostics/reports")
    async def list_reports(request: Request):
        p = await entry(request)
        try:
            limit = int(request.query_params.get("limit") or 50)
        except ValueError:
            raise hz.error(422, "validation_failed", "limit is a number.")
        return await call(diag.service_for(hub_dir).list, hub_ids=visible_hubs(p), limit=limit,
                          cursor=request.query_params.get("cursor"))

    @app.get("/api/admin/diagnostics/reports/{report_id}")
    async def get_report(report_id: str, request: Request):
        p = await entry(request)
        await owned_report(p, report_id)
        return await call(diag.service_for(hub_dir).get, report_id)

    @app.post("/api/admin/diagnostics/reports/{report_id}/cancel")
    async def cancel_report(report_id: str, request: Request):
        p = await authz.require_request(request, hub_dir, "admin.access", mutation=True)
        await owned_report(p, report_id)
        return await call(diag.service_for(hub_dir).cancel, report_id, actor=p.email)

    @app.post("/api/admin/diagnostics/reports/{report_id}/bundles", status_code=201)
    async def make_bundle(report_id: str, request: Request):
        p = await authz.require_request(request, hub_dir, "admin.access", mutation=True)
        await owned_report(p, report_id)
        hub = await call(diag.service_for(hub_dir).hub_of, report_id)
        scope_hub(p, hub, "admin.diagnostics.export")
        return await call(diag.service_for(hub_dir).bundle, report_id, actor=p.email)

    @app.get("/api/admin/diagnostics/bundles/{bundle_id}")
    async def bundle_info(bundle_id: str, request: Request):
        p = await entry(request)
        svc = diag.service_for(hub_dir)
        await owned_report(p, await call(svc.bundle_report, bundle_id))
        return await call(svc.bundle_info, bundle_id)

    @app.get("/api/admin/diagnostics/bundles/{bundle_id}/download")
    async def bundle_download(bundle_id: str, request: Request):
        p = await entry(request)
        svc = diag.service_for(hub_dir)
        report_id = await call(svc.bundle_report, bundle_id)
        await owned_report(p, report_id)
        hub = await call(svc.hub_of, report_id)
        scope_hub(p, hub, "admin.diagnostics.export")
        data = await call(svc.bundle_bytes, bundle_id, actor=p.email)
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="hubzoid-support-{bundle_id[:8]}.zip"',
            "Cache-Control": "no-store"})

    @app.delete("/api/admin/diagnostics/bundles/{bundle_id}")
    async def bundle_delete(bundle_id: str, request: Request):
        p = await authz.require_request(request, hub_dir, "admin.access", mutation=True)
        svc = diag.service_for(hub_dir)
        report_id = await call(svc.bundle_report, bundle_id)
        await owned_report(p, report_id)
        scope_hub(p, await call(svc.hub_of, report_id), "admin.diagnostics.export")
        return await call(svc.delete_bundle, bundle_id, actor=p.email)
