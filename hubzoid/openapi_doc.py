"""The public API reference: ``/docs``, ``/openapi.json`` and ``/api/openapi.json``.

FastAPI describes every route, internal ones included. The reference lists only
what another program may call (docs/api.md): the OpenAI-compatible chat, the
embedded-chat (AG-UI) endpoints, conversations and their files, approvals and
questions, and who the caller is. It carries the package version and the
bearer credentials those routes take. Every address that serves the schema
serves this one.
"""
from __future__ import annotations

from typing import Any

#: Path prefixes of the public API. Anything else is the web app's own.
PUBLIC = ("/v1/", "/api/agui", "/api/conversations", "/api/chat/files", "/api/chat/artifacts",
          "/api/chat/search", "/api/chat/models", "/api/requests", "/api/auth/session",
          "/api/me/access", "/api/shares")
_NEVER = ("/internal", "/api/parity", "/api/admin", "/portal")

DESCRIPTION = """\
What another program can call on a Hubzoid agent. A guide with examples is
in [docs/api.md](https://github.com/hubzoid/hubzoid/blob/main/docs/api.md).

**Address.** Each agent has its own base: `https://<your hub>/b/<agent>`. The
first agent also answers at the root. `GET /b/<agent>/v1/models` names the
model id to send.

**Credentials.** `Authorization: Bearer hzp_...`, a personal API key a person
creates in Settings, Account, Security. It acts as that person, within their
access. An embedded app (AG-UI) uses the short-lived pass its server gets with
`hubzoid embed`. Approvals and questions are answered by a person, never by a
key.
"""


#: Maintenance routes inside public prefixes.
_MAINTENANCE = ("/api/chat/files/doctor", "/api/chat/search/rebuild")


def public(path: str) -> bool:
    return path.startswith(PUBLIC) and not path.startswith(_NEVER + _MAINTENANCE)


def install(app) -> None:
    """Replace ``app.openapi`` with the public reference (built once)."""
    from fastapi.openapi.utils import get_openapi

    from . import __version__

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        routes = [r for r in app.routes if public(getattr(r, "path", "") or "")]
        schema = get_openapi(title="Hubzoid API", version=__version__, description=DESCRIPTION,
                             routes=routes)
        schema.setdefault("components", {})["securitySchemes"] = {
            "personalKey": {"type": "http", "scheme": "bearer",
                            "description": "A personal API key (hzp_...), or an embedded app's pass."}}
        schema["security"] = [{"personalKey": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi
