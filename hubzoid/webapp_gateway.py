"""Agents, branding and groups for the web app.

BASELINE (single hub). Lane E extends this module for gateways (every permitted
agent across the deployment, per-hub ``api_base``), groups and branding assets.

Routes:
  GET /api/agents          agents the signed-in person may use
  GET /api/branding        name and logo URLs for the page chrome
  GET /branding/{file}     the hub's branding files (public: the sign-in page needs them)
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import auth
from .access.identity import normalize

log = logging.getLogger("hubzoid.webapp")

_LOGO_CANDIDATES = ("logo.svg", "logo.png", "favicon.svg", "favicon.png")
_FAVICON_CANDIDATES = ("favicon.svg", "favicon.png", "favicon.ico", "logo.png")


def _slugify(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (text or "").strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "agent"


def agent_card(hub_dir: Path, *, model_label: str | None = None, api_base: str = "") -> dict:
    """What the picker shows for one hub's main agent."""
    from .loaders import agents as agents_loader

    try:
        main = agents_loader.load_main(hub_dir)
        name, description = main.spec.name, main.spec.description
        suggestions = list(main.spec.suggestions)
    except Exception:  # noqa: BLE001 — a broken AGENTS.md still gets a card
        log.warning("webapp: could not load the main agent of %s", hub_dir.name)
        name, description, suggestions = hub_dir.name, "", []
    logo = next((f for f in _LOGO_CANDIDATES if (hub_dir / "branding" / f).is_file()), None)
    return {
        "id": model_label or _slugify(name),
        "name": name,
        "description": description,
        "suggestions": suggestions,
        "hub": hub_dir.name.lower(),
        "api_base": api_base,
        "avatar_url": f"{api_base}/branding/{logo}" if logo else None,
    }


def _can_use(hub_dir: Path, email: str) -> bool:
    from .access import store_for
    from .access.store import USE_HUB

    gs = store_for(hub_dir)
    if gs.is_suspended(email):
        return False
    if not gs.is_authoritative(hub_dir.name):
        return True
    return gs.can(normalize(email), hub_dir.name, USE_HUB)


def mount(app: FastAPI, hub_dir: Path, **ctx) -> None:
    model_label = ctx.get("model_label")

    @app.get("/api/agents")
    async def list_agents(request: Request):
        user = auth.require_user(request, hub_dir)
        card = agent_card(hub_dir, model_label=model_label)
        try:
            allowed = _can_use(hub_dir, user.email)
        except Exception:  # noqa: BLE001 — fail closed
            log.exception("webapp: access check failed")
            raise HTTPException(503, detail={"code": "access_unavailable",
                                             "message": "Access check unavailable. Try again shortly."})
        agents = [card] if allowed else []
        return JSONResponse({"agents": agents,
                             "default_agent": agents[0]["id"] if agents else None})

    @app.get("/api/branding")
    async def branding():
        card = agent_card(hub_dir, model_label=model_label)
        logo = next((f for f in _LOGO_CANDIDATES if (hub_dir / "branding" / f).is_file()), None)
        favicon = next((f for f in _FAVICON_CANDIDATES if (hub_dir / "branding" / f).is_file()), None)
        return JSONResponse({
            "name": card["name"],
            "logo_url": f"/branding/{logo}" if logo else None,
            "favicon_url": f"/branding/{favicon}" if favicon else None,
            "custom_css_url": "/branding/custom.css" if (hub_dir / "branding" / "custom.css").is_file() else None,
        })

    @app.get("/branding/{filename}")
    async def branding_file(filename: str):
        base = (hub_dir / "branding").resolve()
        target = (base / filename).resolve()
        if base not in target.parents or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(str(target), headers={"Cache-Control": "public, max-age=300"})
