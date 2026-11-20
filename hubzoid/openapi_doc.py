"""The public API reference: ``/docs``, ``/openapi.json`` and ``/api/openapi.json``.

FastAPI describes every route, internal ones included. The reference lists
only what another program may call (docs/api.md), as an explicit list of
(method, path) pairs, each marked ``stable`` or ``beta``. Each operation also
says which API key operations may call it (``x-hubzoid-key-operations``,
computed from ``auth.api_keys.allows``, so it can't drift from the rule), and
the main routes carry their request and response shapes (``api_schemas``).
Every address that serves the schema serves this one.

How others do it: Agno serves FastAPI's schema with a response model per route
and a route-to-scope map (``agno/os/scopes.py``); Mastra declares each route
with zod schemas and builds its OpenAPI document and client types from them.
"""
from __future__ import annotations

import re
from typing import Any

STABLE, BETA = "stable", "beta"

#: (method, path, stability). Anything not listed is the web app's own.
PUBLISHED: tuple[tuple[str, str, str], ...] = (
    # OpenAI-compatible chat
    ("POST", "/v1/chat/completions", STABLE),
    ("GET", "/v1/models", STABLE),
    # Who is calling
    ("GET", "/api/auth/session", STABLE),
    ("GET", "/api/me/access", STABLE),
    # Conversations, runs and their events
    ("GET", "/api/conversations", STABLE),
    ("POST", "/api/conversations", STABLE),
    ("GET", "/api/conversations/{conv_id}", STABLE),
    ("PATCH", "/api/conversations/{conv_id}", STABLE),
    ("DELETE", "/api/conversations/{conv_id}", STABLE),
    ("POST", "/api/conversations/{cid}/operations", STABLE),
    ("GET", "/api/chat/operations/{op_id}", BETA),
    ("GET", "/api/conversations/{cid}/events", STABLE),
    ("GET", "/api/conversations/{cid}/branches", STABLE),
    ("POST", "/api/conversations/{cid}/branches", STABLE),
    ("PUT", "/api/conversations/{cid}/view", STABLE),
    ("GET", "/api/conversations/{cid}/queue", BETA),
    ("POST", "/api/conversations/{cid}/queue", BETA),
    ("PATCH", "/api/conversations/{cid}/queue/{item_id}", BETA),
    ("POST", "/api/conversations/{cid}/queue-control", BETA),
    ("POST", "/api/conversations/{cid}/deletion-preview", BETA),
    ("POST", "/api/conversations/{cid}/save", BETA),
    ("PATCH", "/api/conversations/{cid}/title", STABLE),
    ("PATCH", "/api/conversations/{cid}/organization", BETA),
    ("GET", "/api/conversations/{cid}/collaborators", BETA),
    ("PUT", "/api/conversations/{cid}/collaborators", BETA),
    ("GET", "/api/conversations/{cid}/feedback", BETA),
    ("GET", "/api/conversations/{cid}/messages/{mid}/feedback", BETA),
    ("PUT", "/api/conversations/{cid}/messages/{mid}/feedback", BETA),
    ("DELETE", "/api/conversations/{cid}/messages/{mid}/feedback", BETA),
    ("POST", "/api/chat", BETA),
    ("GET", "/api/chat/runs/{run_id}", STABLE),
    ("GET", "/api/runs/{message_id}", BETA),
    ("POST", "/api/runs/{run_ref}/cancel", BETA),
    ("GET", "/api/chat/models", STABLE),
    ("GET", "/api/chat/search", STABLE),
    ("GET", "/api/chat/search/status", BETA),
    # Files and artifacts
    ("POST", "/api/chat/files", STABLE),
    ("GET", "/api/chat/files", STABLE),
    ("GET", "/api/chat/files/{file_id}", STABLE),
    ("GET", "/api/chat/files/{file_id}/content", STABLE),
    ("GET", "/api/chat/files/{file_id}/preview", STABLE),
    ("GET", "/api/chat/files/{file_id}/references", BETA),
    ("DELETE", "/api/chat/files/{file_id}/references/{ref_id}", BETA),
    ("DELETE", "/api/chat/files/{file_id}", STABLE),
    ("POST", "/api/conversations/{cid}/files/reuse", BETA),
    ("POST", "/api/conversations/{conv_id}/files", STABLE),
    ("GET", "/api/conversations/{conv_id}/files/{file_id}", STABLE),
    ("GET", "/api/chat/artifacts", STABLE),
    ("GET", "/api/chat/artifacts/{artifact_id}", STABLE),
    ("POST", "/api/chat/artifacts/{artifact_id}/versions", BETA),
    ("GET", "/api/chat/artifacts/{artifact_id}/versions/{version_id}/content", STABLE),
    # Shares
    ("GET", "/api/shares", STABLE),
    ("POST", "/api/shares", STABLE),
    ("POST", "/api/shares/previews", BETA),
    ("PATCH", "/api/shares/{share_id}", STABLE),
    ("DELETE", "/api/shares/{share_id}", STABLE),
    ("GET", "/api/shares/{token}", STABLE),
    ("GET", "/api/shares/{token}/export", BETA),
    ("POST", "/api/shares/{token}/copies", BETA),
    ("POST", "/api/shares/{token}/copies/previews", BETA),
    ("GET", "/api/conversations/{conv_id}/share", STABLE),
    ("DELETE", "/api/conversations/{conv_id}/share", STABLE),
    # Approvals and questions
    ("GET", "/api/requests", STABLE),
    ("GET", "/api/requests/events", STABLE),
    ("GET", "/api/requests/{rid}", STABLE),
    ("POST", "/api/requests/{rid}/decisions", STABLE),
    ("POST", "/api/requests/{rid}/cancel", STABLE),
    ("GET", "/portal/api/hubs/{hub}/operations", BETA),
    ("GET", "/portal/api/hubs/{hub}/operations/{op_id}", BETA),
    ("POST", "/portal/api/hubs/{hub}/operations/{op_id}/approve", BETA),
    ("POST", "/portal/api/hubs/{hub}/operations/{op_id}/deny", BETA),
    ("POST", "/portal/api/hubs/{hub}/operations/{op_id}/resolve", BETA),
    # Hub changes and personal memory
    ("GET", "/api/hub-changes", BETA),
    ("POST", "/api/hub-changes", BETA),
    ("GET", "/api/hub-changes/{cid}", BETA),
    ("POST", "/api/hub-changes/{cid}/decision", BETA),
    ("POST", "/api/hub-changes/{cid}/undo", BETA),
    ("GET", "/api/memory", BETA),
    ("PUT", "/api/memory", BETA),
    ("DELETE", "/api/memory", BETA),
    # Workflows: your own runs and schedules, team runs
    ("GET", "/portal/api/my", BETA),
    ("GET", "/portal/api/my/runs", BETA),
    ("POST", "/portal/api/my/runs", BETA),
    ("GET", "/portal/api/my/schedules/{schedule_id}", BETA),
    ("PUT", "/portal/api/my/schedules/{schedule_id}", BETA),
    ("GET", "/portal/api/my/schedules/{schedule_id}/runs", BETA),
    ("POST", "/portal/api/my/schedules/{schedule_id}/{action}", BETA),
    ("GET", "/portal/api/workflows", BETA),
    ("POST", "/portal/api/workflows/run", BETA),
    ("GET", "/portal/api/runs", BETA),
    # Notification webhooks
    ("GET", "/api/chat/notification-targets", BETA),
    ("PUT", "/api/chat/notification-targets/{target_id}", BETA),
    ("DELETE", "/api/chat/notification-targets/{target_id}", BETA),
    ("POST", "/api/chat/notification-targets/{target_id}/verify", BETA),
    ("GET", "/api/chat/notification-deliveries", BETA),
    # Chat inside your app (AG-UI)
    ("POST", "/api/agui", STABLE),
    ("GET", "/api/agui/threads", STABLE),
    ("GET", "/api/agui/threads/{thread_id}", STABLE),
    ("GET", "/api/agui/threads/{thread_id}/run", STABLE),
    ("POST", "/api/agui/threads/{thread_id}/stop", STABLE),
    ("DELETE", "/api/agui/threads/{thread_id}", STABLE),
)

_INDEX = {(m, p): s for m, p, s in PUBLISHED}
_PATHS = {p for _m, p, _s in PUBLISHED}

DESCRIPTION = """\
What another program can call on a Hubzoid agent. A guide with examples is
in [docs/api.md](https://github.com/hubzoid/hubzoid/blob/main/docs/api.md).

**Address.** Each agent has its own base: `https://<your hub>/b/<agent>`. The
first agent also answers at the root. `GET /b/<agent>/v1/models` names the
model id to send.

**Credentials.** `Authorization: Bearer hzp_...`, a personal API key a person
creates in Settings, Account, Security. It acts as that person, within their
access and the key's operations (each route lists the operations that may
call it in `x-hubzoid-key-operations`). Approvals and questions are decided
by a person, or by a key its owner made with the `approve` operation. A
machine (a CI job, a cloud workload) can send a JWT from its own issuer when
an administrator registered it as an API app. An embedded app (AG-UI) uses
the short-lived pass its server gets with `hubzoid embed`.

**Stability.** `x-hubzoid-stability: stable` routes keep their paths, methods,
required fields and response fields for at least one minor release after a
change is announced in the changelog. `beta` routes may still change.
"""


def public(path: str, method: str | None = None) -> bool:
    """Whether this route (and method) is in the published reference."""
    if method is None:
        return path in _PATHS
    return (method.upper(), path) in _INDEX


def _sample(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def key_operations(method: str, path: str) -> list[str]:
    """The API key operations that may call this route (``api_keys.allows``)."""
    from .auth import api_keys

    return [op for op in api_keys.OPERATIONS if api_keys.allows((op,), method, _sample(path))]


def _model_schema(model, components: dict) -> dict:
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    for name, sub in (schema.pop("$defs", None) or {}).items():
        components.setdefault(name, sub)
    components[model.__name__] = schema
    return {"$ref": f"#/components/schemas/{model.__name__}"}


def _shape(op: dict, shape: dict, components: dict) -> None:
    request = shape.get("request")
    if request is not None:
        if isinstance(request, tuple) and request[0] == "multipart":
            ref = _model_schema(request[1], components)
            op["requestBody"] = {"required": True, "content": {"multipart/form-data": {"schema": ref}}}
        else:
            op["requestBody"] = {"required": True,
                                 "content": {"application/json": {"schema": _model_schema(request, components)}}}
    response = shape.get("response")
    if response is None:
        return
    status = shape.get("status", "200")
    responses = op.setdefault("responses", {})
    if isinstance(response, tuple) and response[0] == "sse":
        schema = _model_schema(response[1], components) if response[1] else {"type": "string"}
        responses[status] = {"description": "Server-Sent Events.", "content": {"text/event-stream": {"schema": schema}}}
    else:
        responses[status] = {"description": "OK",
                             "content": {"application/json": {"schema": _model_schema(response, components)}}}
    if status != "200":
        responses.pop("200", None)


def build(app) -> dict[str, Any]:
    """The published reference for ``app``."""
    from fastapi.openapi.utils import get_openapi

    from . import __version__, api_schemas

    routes = [r for r in app.routes if public(getattr(r, "path", "") or "")]
    schema = get_openapi(title="Hubzoid API", version=__version__, description=DESCRIPTION, routes=routes)
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for path, ops in list(schema.get("paths", {}).items()):
        for method in list(ops):
            if not public(path, method):
                del ops[method]
                continue
            op = ops[method]
            op["x-hubzoid-stability"] = _INDEX[(method.upper(), path)]
            op["x-hubzoid-key-operations"] = key_operations(method.upper(), path)
            shape = api_schemas.SHAPES.get((method.upper(), path))
            if shape:
                _shape(op, shape, components)
        if not ops:
            del schema["paths"][path]
    for model in api_schemas.EXTRA:
        _model_schema(model, components)
    schema["components"]["securitySchemes"] = {
        "personalKey": {"type": "http", "scheme": "bearer",
                        "description": "A personal API key (hzp_...), a machine JWT from a registered API app, "
                                       "or an embedded app's pass."}}
    schema["security"] = [{"personalKey": []}]
    return schema


def install(app) -> None:
    """Replace ``app.openapi`` with the public reference (built once)."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        app.openapi_schema = build(app)
        return app.openapi_schema

    app.openapi = openapi
