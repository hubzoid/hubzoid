"""Agents, branding and groups for the web app, for one hub or a whole gateway.

Mounted by ``hubzoid.webapp.mount`` in the web app mode only (never in the
legacy Open WebUI mode), before the Console's static files, so the
``/portal/api/groups`` routes are not shadowed.

Routes:
  GET    /api/agents                           agents the signed-in person may use
  GET    /api/branding                         name and asset URLs for the page chrome
  GET    /branding/{file}                      branding files (public: the sign-in page needs them)
  GET    /portal/api/groups                    groups            (organization administrators)
  POST   /portal/api/groups                    {name, description?, emails?}
  GET    /portal/api/groups/{id}               one group, its members and its access
  PATCH  /portal/api/groups/{id}               {name?, description?}
  DELETE /portal/api/groups/{id}               also removes every grant it holds
  POST   /portal/api/groups/{id}/members       {emails}
  DELETE /portal/api/groups/{id}/members/{email}

One hub (``hubzoid run``): one agent card, ``api_base`` "" and the hub's
``branding/`` folder.

Gateway (this hub is registered in a deployment manifest): every registered hub
is a card, filtered per hub by suspension and ``use_hub`` (a hub whose access
is not yet managed in the Console is open to everyone signed in, as its bridge
enforces), with ``api_base`` ``/b/<slug>`` and its avatar under
``/b/<slug>/branding/``. The page chrome uses the gateway's branding folder:
the one the gateway recorded in the manifest (from ``HUBZOID_GATEWAY_BRANDING``,
a hub slug or a path, then ``<gateway data dir>/branding`` when it holds
assets, then the first hub's), computed the same way when nothing is
recorded. The edge forwards ``/b/<slug>/branding/*`` to that hub's bridge with
the prefix stripped and ``X-Forwarded-Prefix: /b/<slug>``; a bridge seeing its
own prefix serves its own hub's branding.

Access decisions fail closed: a store or manifest error answers 503, never a
wider list. Database and file work runs in the threadpool, so a slow read
(a busy SQLite file) never stalls the bridge's other requests.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import auth
from .access.identity import normalize

log = logging.getLogger("hubzoid.webapp")

_LOGO_CANDIDATES = ("logo.svg", "logo.png", "logo.webp", "favicon.svg", "favicon.png")
_FAVICON_CANDIDATES = ("favicon.svg", "favicon.png", "favicon.ico", "logo.svg", "logo.png")
_PREFIX_HEADER = "x-forwarded-prefix"


def _slugify(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (text or "").strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "agent"


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)


# ---------------------------------------------------------------------------
# Deployment shape
# ---------------------------------------------------------------------------
def _manifest(hub_dir: Path) -> dict:
    """The deployment manifest this hub is registered in, or {} standalone.
    Raises when a manifest exists but can't be used (fail closed upstream)."""
    from . import deployment

    return deployment.read(Path(hub_dir)) or {}


def _hub_slug(entry: dict) -> str:
    return entry.get("slug") or _slugify(Path(entry["path"]).name)


def own_entry(hub_dir: Path, manifest: dict) -> dict | None:
    """This hub's entry in the manifest, or None standalone."""
    here = Path(hub_dir).resolve()
    for entry in manifest.get("hubs") or []:
        if Path(entry["path"]).resolve() == here:
            return entry
    return None


def api_base_for(hub_dir: Path) -> str:
    """Where the chat app calls this hub's hub-scoped routes: "" for a
    standalone hub, "/b/<slug>" in a gateway (the manifest's `slug`, or the
    slugified folder name for a manifest written before slugs were recorded).
    Raises when a manifest exists but can't be used."""
    entry = own_entry(Path(hub_dir), _manifest(Path(hub_dir)))
    return f"/b/{_hub_slug(entry)}" if entry is not None else ""


def _find(folder: Path, candidates: tuple[str, ...]) -> str | None:
    """The real file name of the first candidate present (case-insensitive)."""
    try:
        files = {p.name.lower(): p.name for p in folder.iterdir() if p.is_file()}
    except OSError:
        return None
    for name in candidates:
        if name in files:
            return files[name]
    return None


# ---------------------------------------------------------------------------
# Agent cards
# ---------------------------------------------------------------------------
def agent_card(hub_dir: Path, *, model_label: str | None = None, api_base: str = "",
               hub_key: str | None = None, fallback_name: str | None = None) -> dict:
    """What the picker shows for one hub's main agent."""
    from .loaders import agents as agents_loader

    hub_dir = Path(hub_dir)
    try:
        main = agents_loader.load_main(hub_dir)
        name, description = main.spec.name, main.spec.description
        suggestions = list(main.spec.suggestions)
    except Exception:  # noqa: BLE001 — a broken AGENTS.md still gets a card
        log.warning("webapp: could not load the main agent of %s", hub_dir.name)
        name, description, suggestions = fallback_name or hub_dir.name, "", []
    logo = _find(hub_dir / "branding", _LOGO_CANDIDATES)
    return {
        "id": model_label or _slugify(name),
        "name": name,
        "description": description,
        "suggestions": suggestions,
        "hub": normalize(hub_key or hub_dir.name),
        "api_base": api_base,
        "avatar_url": f"{api_base}/branding/{logo}" if logo else None,
    }


def _may_use(gs, hub_key: str, email: str) -> bool:
    """Entry to one hub, as its bridge decides it (`server._enforce_use_hub`):
    a hub still on legacy access is open to everyone signed in; a managed hub
    needs `use_hub` (directly, through a group, or public)."""
    from .access.store import USE_HUB

    if not gs.is_authoritative(hub_key):
        return True
    return gs.can(email, hub_key, USE_HUB)


def agents_for(hub_dir: Path, email: str, *, model_label: str | None = None) -> list[dict]:
    """Every agent `email` may use in this deployment, in manifest order.
    Raises on a store or manifest error (callers answer 503)."""
    from .access import store_for

    hub_dir = Path(hub_dir)
    email = normalize(email)
    gs = store_for(hub_dir)
    if not email or gs.is_suspended(email):
        return []
    manifest = _manifest(hub_dir)
    hubs = manifest.get("hubs") or []
    if not hubs:
        key = normalize(hub_dir.name)
        return [agent_card(hub_dir, model_label=model_label, hub_key=key)] if _may_use(gs, key, email) else []
    cards = []
    for entry in hubs:
        key = normalize(entry["key"])
        if not _may_use(gs, key, email):
            continue
        cards.append(agent_card(Path(entry["path"]), model_label=entry.get("model_id"),
                                api_base=f"/b/{_hub_slug(entry)}", hub_key=key,
                                fallback_name=entry.get("name")))
    return cards


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------
def gateway_branding_dir(manifest: dict, env=None) -> Path | None:
    """The gateway's branding folder: recorded in the manifest by the gateway,
    else HUBZOID_GATEWAY_BRANDING (a hub slug or a path holding `branding/`),
    else `<gateway data dir>/branding` when it holds assets, else the first hub's."""
    from . import branding as branding_lib

    env = os.environ if env is None else env
    recorded = manifest.get("branding_dir")
    if recorded:
        return Path(recorded)
    hubs = manifest.get("hubs") or []
    override = (env.get("HUBZOID_GATEWAY_BRANDING") or "").strip()
    if override:
        for entry in hubs:
            if _hub_slug(entry) == override:
                return Path(entry["path"]) / "branding"
        candidate = Path(override).expanduser()
        if candidate.is_dir():
            return candidate / "branding"
    data_dir = manifest.get("_dir")
    if data_dir and branding_lib.has_assets(Path(data_dir) / "branding"):
        return Path(data_dir) / "branding"
    if hubs:
        return Path(hubs[0]["path"]) / "branding"
    return None


def _manifest_with_dir(hub_dir: Path) -> dict:
    """The manifest plus `_dir`, the gateway data directory it lives in."""
    import json

    manifest = _manifest(hub_dir)
    if manifest:
        path = os.environ.get("HUBZOID_DEPLOYMENT")
        if not path:
            pointer = Path(hub_dir) / ".hubzoid" / "deployment.json"
            try:
                path = json.loads(pointer.read_text()).get("manifest")
            except (OSError, ValueError):
                path = None
        if path:
            manifest = dict(manifest, _dir=str(Path(path).parent))
    return manifest


class _Scope:
    """Which branding a request is about: the deployment's (the page chrome) or,
    for `/b/<slug>/...` forwarded by the edge, this hub's own."""

    def __init__(self, folder: Path | None, url_base: str, name: str):
        self.folder = folder
        self.url_base = url_base
        self.name = name


def branding_scope(hub_dir: Path, request: Request | None, *, model_label: str | None = None) -> _Scope:
    hub_dir = Path(hub_dir)
    manifest = _manifest_with_dir(hub_dir)
    entry = own_entry(hub_dir, manifest)
    prefix = (request.headers.get(_PREFIX_HEADER) or "").rstrip("/") if request is not None else ""
    if entry is not None and prefix and prefix == f"/b/{_hub_slug(entry)}":
        card = agent_card(hub_dir, model_label=model_label, fallback_name=entry.get("name"))
        return _Scope(hub_dir / "branding", prefix, card["name"])
    if entry is not None:
        return _Scope(gateway_branding_dir(manifest), "",
                      str(manifest.get("name") or "Hubzoid"))
    card = agent_card(hub_dir, model_label=model_label)
    return _Scope(hub_dir / "branding", "", card["name"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def mount(app: FastAPI, hub_dir: Path, **ctx) -> None:
    hub_dir = Path(hub_dir)
    model_label = ctx.get("model_label")

    @app.get("/api/agents")
    async def list_agents(request: Request):
        user = await run_in_threadpool(auth.require_user, request, hub_dir)
        try:
            agents = await run_in_threadpool(agents_for, hub_dir, user.email, model_label=model_label)
        except Exception:  # noqa: BLE001 — fail closed
            log.exception("webapp: access check failed")
            raise HTTPException(503, detail={"code": "access_unavailable",
                                             "message": "Access check unavailable. Try again shortly."})
        return JSONResponse({"agents": agents,
                             "default_agent": agents[0]["id"] if agents else None})

    def branding_payload(request: Request) -> dict:
        try:
            scope = branding_scope(hub_dir, request, model_label=model_label)
        except Exception:  # noqa: BLE001 — chrome falls back to the product name
            log.exception("webapp: branding unavailable")
            return {"name": "Hubzoid", "logo_url": None, "favicon_url": None,
                    "custom_css_url": None}
        folder = scope.folder
        logo = _find(folder, _LOGO_CANDIDATES) if folder else None
        favicon = _find(folder, _FAVICON_CANDIDATES) if folder else None
        css = _find(folder, ("custom.css",)) if folder else None
        base = scope.url_base
        return {
            "name": scope.name,
            "logo_url": f"{base}/branding/{logo}" if logo else None,
            "favicon_url": f"{base}/branding/{favicon}" if favicon else None,
            "custom_css_url": f"{base}/branding/{css}" if css else None,
        }

    @app.get("/api/branding")
    async def branding(request: Request):
        return JSONResponse(await run_in_threadpool(branding_payload, request))

    def branding_path(filename: str, request: Request) -> Path | None:
        try:
            scope = branding_scope(hub_dir, request, model_label=model_label)
        except Exception:  # noqa: BLE001
            log.exception("webapp: branding unavailable")
            return None
        if scope.folder is None:
            return None
        base = scope.folder.resolve()
        real = _find(base, (filename.lower(),))
        if real is None:
            return None
        target = (base / real).resolve()
        if base not in target.parents or not target.is_file():
            return None
        return target

    @app.get("/branding/{filename}")
    async def branding_file(filename: str, request: Request):
        if not filename or filename.startswith("."):
            raise HTTPException(404, "not found")
        target = await run_in_threadpool(branding_path, filename, request)
        if target is None:
            raise HTTPException(404, "not found")
        return FileResponse(str(target), headers={"Cache-Control": "public, max-age=300"})

    _mount_groups(app, hub_dir)


def _mount_groups(app: FastAPI, hub_dir: Path) -> None:
    """The Console's group routes (contract 6.7). Organization administrators
    only (decided by the access store), same-origin mutations."""
    from pydantic import BaseModel, ConfigDict, Field, ValidationError

    from .access.service import Actor
    from .groups import UNSET, GroupRefused, GroupService

    service = GroupService(hub_dir)

    class CreateBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str = Field(max_length=400)
        description: str | None = Field(default=None, max_length=2000)
        emails: list[str] = Field(default_factory=list, max_length=200)

    class UpdateBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str | None = Field(default=None, max_length=400)
        description: str | None = Field(default=None, max_length=2000)

    class MembersBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        emails: list[str] = Field(min_length=1, max_length=200)

    def actor(request: Request) -> Actor:
        user = auth.require_user(request, hub_dir)
        return Actor(normalize(user.email), "console", "session")

    def same_origin(request: Request) -> None:
        from .access.session import require_same_origin

        try:
            require_same_origin(request)
        except HTTPException as exc:
            raise GroupRefused(403, "cross_origin",
                               "This change must come from the Console's own page.") from exc

    async def body(request: Request, model):
        try:
            raw = await request.json()
        except Exception:  # noqa: BLE001
            raise GroupRefused(422, "invalid_request", "Send a JSON body.")
        try:
            return model.model_validate(raw if raw is not None else {})
        except ValidationError as exc:
            fields = sorted({".".join(str(x) for x in e["loc"]) or "body" for e in exc.errors()})
            raise GroupRefused(422, "invalid_request", "Check these fields: " + ", ".join(fields) + ".")

    def refused(fn):
        import functools

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except GroupRefused as exc:
                return _error(exc.status, exc.code, exc.message)

        return wrapper

    # Every service call reads or writes the store: run it in the threadpool.
    def call(fn, *args, **kwargs):
        return run_in_threadpool(fn, *args, **kwargs)

    @app.get("/portal/api/groups")
    @refused
    async def list_groups(request: Request):
        who = await call(actor, request)
        return JSONResponse({"groups": await call(service.list, who)})

    @app.post("/portal/api/groups")
    @refused
    async def create_group(request: Request):
        who = await call(actor, request)
        same_origin(request)
        payload = await body(request, CreateBody)
        group = await call(service.create, who, name=payload.name,
                           description=payload.description, emails=payload.emails)
        return JSONResponse({"group": group}, status_code=201)

    @app.get("/portal/api/groups/{group_id}")
    @refused
    async def get_group(group_id: str, request: Request):
        who = await call(actor, request)
        return JSONResponse({"group": await call(service.get, who, group_id)})

    @app.patch("/portal/api/groups/{group_id}")
    @refused
    async def update_group(group_id: str, request: Request):
        who = await call(actor, request)
        same_origin(request)
        payload = await body(request, UpdateBody)
        description = payload.description if "description" in payload.model_fields_set else UNSET
        group = await call(service.update, who, group_id, name=payload.name, description=description)
        return JSONResponse({"group": group})

    @app.delete("/portal/api/groups/{group_id}")
    @refused
    async def delete_group(group_id: str, request: Request):
        who = await call(actor, request)
        same_origin(request)
        await call(service.delete, who, group_id)
        return Response(status_code=204)

    @app.post("/portal/api/groups/{group_id}/members")
    @refused
    async def add_members(group_id: str, request: Request):
        who = await call(actor, request)
        same_origin(request)
        payload = await body(request, MembersBody)
        group = await call(service.add_members, who, group_id, payload.emails)
        return JSONResponse({"group": group, "added": group.pop("added", [])})

    @app.delete("/portal/api/groups/{group_id}/members/{email}")
    @refused
    async def remove_member(group_id: str, email: str, request: Request):
        who = await call(actor, request)
        same_origin(request)
        await call(service.remove_member, who, group_id, email)
        return Response(status_code=204)
