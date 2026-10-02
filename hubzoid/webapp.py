"""The Hubzoid web app on the bridge: one page for chat and Console.

``mount`` runs in the default UI mode (not Open WebUI mode). It
registers, in order:

  1. sign-in routes          hubzoid.auth.routes.mount        (/api/auth, /oauth)
  2. chat routes             hubzoid.chat.routes.mount        (/api/chat, /api/conversations, /api/shares)
  3. connection routes       hubzoid.connectors.routes.mount  (/api/connections, /portal/api/connectors)
  4. gateway and groups      hubzoid.webapp_gateway.mount     (/api/agents, /api/branding, /portal/api/groups)
  5. the page shell          GET /, /c/*, /s/*, /auth*, /account*

It must run BEFORE ``portal.mount_portal``: the Console's static files are
mounted at ``/portal`` and would otherwise shadow any later ``/portal/api/...``
route. The built bundle lives in ``hubzoid/portal_dist`` (Vite ``base: /portal/``),
so the page served at ``/`` loads its assets from ``/portal/assets``.
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

log = logging.getLogger("hubzoid.webapp")

DIST = Path(__file__).parent / "portal_dist"

# Client-side routes of the chat app. The Console keeps its hash routes under /portal/.
SHELL_PATHS = ("/", "/c/{rest:path}", "/s/{rest:path}", "/auth", "/auth/{rest:path}",
               "/account", "/account/{rest:path}", "/new", "/new/{rest:path}")

_MOUNTS = (
    "hubzoid.auth.routes",
    "hubzoid.chat.routes",
    "hubzoid.connectors.routes",
    "hubzoid.webapp_gateway",
)

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


def _shell() -> Response:
    index = DIST / "index.html"
    if index.is_file():
        return FileResponse(str(index), media_type="text/html", headers=_NO_CACHE)
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Hubzoid</title>"
        "<p>The Hubzoid web app is not built in this checkout. "
        "Run <code>npm run build</code> in <code>portal/</code>.</p>",
        status_code=503,
        headers=_NO_CACHE,
    )


def mount(app: FastAPI, hub_dir: Path, **ctx) -> None:
    """Register the web app on the bridge. ``ctx`` carries runtime, inflight and
    settings from ``server.build_app`` for the modules that need them."""
    hub_dir = Path(hub_dir)
    for name in _MOUNTS:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name:
                continue  # optional part not present in this build
            raise
        module.mount(app, hub_dir, **ctx)

    async def shell(request: Request) -> Response:  # noqa: ARG001
        return _shell()

    for path in SHELL_PATHS:
        app.add_api_route(path, shell, methods=["GET"], include_in_schema=False)
    log.info("webapp: mounted (bundle %s)", "present" if (DIST / "index.html").is_file() else "missing")
