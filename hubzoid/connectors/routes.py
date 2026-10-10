"""HTTP for personal connections, in both UI modes (mounted by ``hubzoid.webapp``,
or by the bridge in Open WebUI mode, before the Console's static files so
``/portal/api/connectors`` is not shadowed). Who is signed in comes from a live
check of the session: Hubzoid's, or Open WebUI's in Open WebUI mode.

  Console (organization administrators)
    GET    /portal/api/connectors                 the registry, never a secret
    POST   /portal/api/connectors                 add
    PATCH  /portal/api/connectors/{id}            change (fields present only)
    DELETE /portal/api/connectors/{id}            remove, with every connection to it
    POST   /portal/api/connectors/{id}/test       discovery result, changes nothing

  People (signed in; also under /portal/api/connections, which both modes route here)
    GET    /api/connections                       [{connector_id, name, connected, status,
                                                    connected_at, allowed, ...}]
    POST   /api/connections/{id}/connect          {authorize_url}
    DELETE /api/connections/{id}                  204, revoked at the provider when possible

  Browser
    GET    /oauth/connectors/{id}/callback        302 to return_to, or
                                                  /account/connections?connected=<id> (?error=<code>)

Errors are ``{"detail": {"code", "message"}}``. Every mutation needs a
same-origin request (``access.session.require_same_origin``).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ..access.identity import normalize
from . import ConnectorError, oauth_flow, per_user, registry, tokens
from . import http as net

log = logging.getLogger("hubzoid.connectors")

_NO_STORE = {"Cache-Control": "no-store"}
_REDIRECT_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
ACCOUNT_PAGE = "/account/connections"
PORTAL_PAGE = "/portal/connections"  # Open WebUI mode has no chat-app account pages


def account_page(hub_dir: Path) -> str:
    """Where a person manages their connections in this UI mode."""
    from .. import appmode

    return PORTAL_PAGE if appmode.is_openwebui(hub_dir) else ACCOUNT_PAGE


def signed_in(request: Request, hub_dir: Path):
    """The signed-in account (``AuthUser``) or None. In Open WebUI mode it is
    built from a live check of the Open WebUI session: its account id and
    email, never an email lookup or a forwarded chat header."""
    from .. import appmode
    from ..auth import AuthUser, current_user

    if not appmode.is_openwebui(hub_dir):
        return current_user(request, hub_dir)
    from ..access.session import verified_person

    who = verified_person(request, hub_dir)
    return AuthUser(id=who[0], email=who[1]) if who else None


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _raise(err: ConnectorError):
    raise _error(err.status, err.code, err.message)


def _redirect(target: str) -> RedirectResponse:
    return RedirectResponse(target, status_code=302, headers=_REDIRECT_HEADERS)


async def json_body(request: Request) -> Any:
    """The request's JSON (None when empty), refused in the API's own error
    shape when it is not JSON."""
    raw = await request.body()
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise _error(422, "invalid_request", "Send the request body as JSON.") from None


def build_router(hub_dir: Path) -> APIRouter:
    hub_dir = Path(hub_dir)
    router = APIRouter()

    # ---- who ----------------------------------------------------------------
    def same_origin(request: Request) -> None:
        from ..access.session import require_same_origin

        try:
            require_same_origin(request)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else None
            if detail and "code" in detail:
                raise
            raise _error(403, "cross_origin",
                         "This request must come from this site's own pages.") from None

    def trusted_host(request: Request) -> None:
        """Sign-in off means every request is the owner. Then only answer
        requests addressed to this machine (unless an operator configured the
        public address), so a web page using DNS rebinding cannot act as the
        owner through a visitor's browser."""
        from .. import appmode

        if appmode.auth_enabled(hub_dir) or appmode.allowed_origins():
            return
        if not net.is_loopback_url(oauth_flow.request_origin(request)):
            raise _error(403, "untrusted_host", "Open Hubzoid at its local address, or set "
                                                "HUBZOID_PUBLIC_URL to the address people use.")

    def person(request: Request):
        from .. import appmode
        from ..auth import require_user

        if not appmode.is_openwebui(hub_dir):
            trusted_host(request)
            return require_user(request, hub_dir)
        user = signed_in(request, hub_dir)
        if user is None:
            raise _error(401, "unauthenticated", "Sign in to continue.")
        return user

    def admin(request: Request):
        """Organization administrators, as the Console decides them: an
        administrator account that holds organization-wide Manage access. When
        sign-in is off the local owner is the organization's administrator."""
        from .. import appmode
        from ..access.service import AccessService, Actor, Denied
        from ..auth import LOCAL_OWNER_EMAIL, require_admin

        if appmode.is_openwebui(hub_dir):
            # Open WebUI's own admin role decides nothing here: the Console's
            # organization-wide Manage access does, as for everything else.
            user = person(request)
        else:
            trusted_host(request)
            user = require_admin(request, hub_dir)
            if not appmode.auth_enabled(hub_dir) and normalize(user.email) == LOCAL_OWNER_EMAIL:
                return user
        try:
            scope = AccessService(hub_dir).scope(Actor(normalize(user.email), "console", "session"))
        except Denied as exc:
            raise _error(exc.status, exc.code, exc.message) from None
        if not scope.org_admin:
            raise _error(403, "forbidden", "Only organization administrators manage connectors.")
        return user

    def audit(email: str, connector_id: str, decision: str, reason: str) -> None:
        """One row in the access decision log, like the connection journey."""
        from ..connect_journey import store as journeys

        journeys.audit(hub_dir, hub=normalize(hub_dir.name), subject=email, surface="web",
                       app=connector_id, decision=decision, reason=reason)

    # ---- Console: the registry ------------------------------------------------
    def entry(c: registry.Connector, origin: str, counts: dict) -> dict:
        return {**c.public(), "redirect_uri": oauth_flow.redirect_uri(origin, c.id),
                "connections": counts.get(c.id, 0), "agents": registry.agents_of(hub_dir, c.id)}

    @router.get("/portal/api/connectors")
    def list_connectors(request: Request):
        admin(request)
        origin = oauth_flow.origin_for(request, strict=False)
        counts = tokens.counts(hub_dir)
        return JSONResponse({"connectors": [entry(c, origin, counts)
                                            for c in registry.list_all(hub_dir)]},
                            headers=_NO_STORE)

    @router.post("/portal/api/connectors")
    def create_connector(request: Request, body: Any = Depends(json_body), hub: str = ""):
        """Register a connector, and with ``?hub=`` offer it in that agent."""
        user = admin(request)
        same_origin(request)
        try:
            c = registry.create(hub_dir, body, actor=user.email)
            if hub:
                registry.offer(hub_dir, c.id, hub, actor=user.email)
        except ConnectorError as err:
            _raise(err)
        return JSONResponse({"connector": entry(c, oauth_flow.origin_for(request, strict=False),
                                                {})}, status_code=201, headers=_NO_STORE)

    @router.patch("/portal/api/connectors/{connector_id}")
    def update_connector(connector_id: str, request: Request, body: Any = Depends(json_body)):
        user = admin(request)
        same_origin(request)
        try:
            c, _reset = registry.update(hub_dir, connector_id, body, actor=user.email)
        except ConnectorError as err:
            _raise(err)
        return JSONResponse({"connector": entry(c, oauth_flow.origin_for(request, strict=False),
                                                tokens.counts(hub_dir))}, headers=_NO_STORE)

    @router.delete("/portal/api/connectors/{connector_id}", status_code=204)
    def delete_connector(connector_id: str, request: Request):
        user = admin(request)
        same_origin(request)
        if not registry.delete(hub_dir, connector_id, actor=user.email):
            raise _error(404, "not_found", "No connector has this ID.")
        return Response(status_code=204)

    @router.put("/portal/api/connectors/{connector_id}/agents/{hub}", status_code=204)
    def offer_connector(connector_id: str, hub: str, request: Request):
        """Offer a registered connector in one agent."""
        user = admin(request)
        same_origin(request)
        _known_agent(hub)
        try:
            registry.offer(hub_dir, connector_id, hub, actor=user.email)
        except ConnectorError as err:
            _raise(err)
        return Response(status_code=204)

    @router.delete("/portal/api/connectors/{connector_id}/agents/{hub}", status_code=204)
    def withdraw_connector(connector_id: str, hub: str, request: Request):
        """Stop offering a connector in one agent (its grants there go too)."""
        user = admin(request)
        same_origin(request)
        if not registry.withdraw(hub_dir, connector_id, hub, actor=user.email):
            raise _error(404, "not_found", "This agent does not offer this connector.")
        return Response(status_code=204)

    def _known_agent(hub: str) -> None:
        from .. import deployment

        try:
            keys = {normalize(h["key"]) for h in deployment.hubs(hub_dir)}
        except Exception:  # noqa: BLE001 — a standalone hub without a readable manifest
            keys = {normalize(hub_dir.name)}
        if normalize(hub) not in keys:
            raise _error(404, "unknown_agent", "There is no such agent.")

    @router.post("/portal/api/connectors/{connector_id}/test")
    def test_connector(connector_id: str, request: Request):
        user = admin(request)
        same_origin(request)
        c = registry.get(hub_dir, connector_id)
        if c is None:
            raise _error(404, "not_found", "No connector has this ID.")
        result = _test(hub_dir, c, oauth_flow.origin_for(request, strict=False))
        if result.get("ok"):
            result.update(_tools(hub_dir, c, user))
        return JSONResponse(result, headers=_NO_STORE)

    # ---- People: their own connections -----------------------------------------
    @router.get("/api/connections")
    @router.get("/portal/api/connections")
    def my_connections(request: Request):
        user = person(request)
        mine = {c.connector_id: c for c in tokens.for_user(hub_dir, user.id)}
        listed = [c for c in registry.list_all(hub_dir) if c.enabled or c.id in mine]
        used = per_user.used_by(hub_dir, user.email, [c.id for c in listed if c.enabled])
        out = []
        for c in listed:
            conn = mine.get(c.id)
            out.append({
                "connector_id": c.id, "name": c.name, "connected": conn is not None,
                "status": conn.status if conn else "none",
                "connected_at": conn.connected_at if conn else None,
                "allowed": bool(used.get(c.id)), "auth_type": c.auth_type, "enabled": c.enabled,
                "agents": used.get(c.id, []),
            })
        return JSONResponse(out, headers=_NO_STORE)

    @router.post("/api/connections/{connector_id}/connect")
    @router.post("/portal/api/connections/{connector_id}/connect")
    def connect(connector_id: str, request: Request, body: Any = Depends(json_body)):
        user = person(request)
        same_origin(request)
        if body is not None and not isinstance(body, dict):
            raise _error(422, "invalid_request", "Send a JSON object.")
        raw = (body or {}).get("return_to")
        return_to = oauth_flow.safe_return_to(raw)
        if raw not in (None, "") and return_to is None:
            raise _error(422, "invalid_return_to", "return_to must be a path on this site.")
        c = registry.get(hub_dir, connector_id)
        if c is None:
            raise _error(404, "not_found", "No connector has this ID.")
        if not c.enabled:
            raise _error(409, "disabled", f"{c.name} is switched off. Ask your administrator.")
        if c.id not in per_user.allowed_ids(hub_dir, user.email, [c.id]):
            audit(user.email, c.id, "deny", "not permitted")
            raise _error(403, "not_allowed", f"You do not have permission to connect {c.name}. "
                                             "Ask your administrator.")
        try:
            if c.auth_type == "none":
                oauth_flow.connect_without_auth(hub_dir, c, user)
                audit(user.email, c.id, "connected", "no sign-in needed")
                return JSONResponse({"authorize_url": return_to or
                                     f"{account_page(hub_dir)}?connected={c.id}"},
                                    headers=_NO_STORE)
            url = oauth_flow.start(hub_dir, c, user, origin=oauth_flow.origin_for(request),
                                   return_to=return_to)
        except ConnectorError as err:
            _raise(err)
        return JSONResponse({"authorize_url": url}, headers=_NO_STORE)

    @router.delete("/api/connections/{connector_id}", status_code=204)
    @router.delete("/portal/api/connections/{connector_id}", status_code=204)
    def disconnect(connector_id: str, request: Request):
        user = person(request)
        same_origin(request)
        if not registry.ID_RE.match(connector_id or ""):
            raise _error(404, "not_found", "No connector has this ID.")
        if tokens.disconnect(hub_dir, user.id, connector_id):
            audit(user.email, connector_id, "disconnected", "disconnected by the person")
        return Response(status_code=204)

    # ---- Browser: the provider sends the person back here ------------------------
    @router.get("/oauth/connectors/{connector_id}/callback")
    def callback(connector_id: str, request: Request):
        q = request.query_params
        page = account_page(hub_dir)
        fallback = oauth_flow.with_params(page, {"connector": connector_id})
        try:
            user = signed_in(request, hub_dir)
        except HTTPException:
            return _redirect(oauth_flow.with_params(fallback, {"error": "unavailable"}))
        if user is None:
            # Signed out mid-way: nothing is consumed; the link expires by itself.
            return _redirect(oauth_flow.with_params(fallback, {"error": "unauthenticated"}))
        try:
            done = oauth_flow.callback(hub_dir, connector_id=connector_id, user=user,
                                       state=q.get("state"), code=q.get("code"),
                                       iss=q.get("iss"), error=q.get("error"))
        except oauth_flow.CallbackError as err:
            decision = "deny" if err.code in ("wrong_user", "issuer_mismatch") else "failed"
            audit(user.email, connector_id, decision, err.code)
            _journey_failed(hub_dir, err.journey_id, err.code)
            target = err.return_to if err.return_to and err.code != "wrong_user" else fallback
            return _redirect(oauth_flow.with_params(target, {"error": err.code}))
        except ConnectorError as err:
            audit(user.email, connector_id, "failed", err.code)
            return _redirect(oauth_flow.with_params(fallback, {"error": err.code}))
        audit(user.email, connector_id, "connected", "authorized with the provider")
        return _redirect(done.return_to or oauth_flow.with_params(page,
                                                                  {"connected": connector_id}))

    return router


def _journey_failed(hub_dir: Path, journey_id: str | None, code: str) -> None:
    """A connection journey whose authorization failed ends as failed at once,
    so its done page and chat say so instead of waiting out the link."""
    if not journey_id:
        return
    try:
        from ..connect_journey import store as journeys

        j = journeys.get(hub_dir, journey_id)
        if j and journeys.transition(hub_dir, journey_id, frm=("started",), to="failed"):
            journeys.audit(hub_dir, hub=j["hub"], subject=j["subject"], surface=j["surface"],
                           app=j["app"], decision="failed", reason=code)
    except Exception:  # noqa: BLE001 — the journey expires by itself
        log.debug("connectors: could not close journey", exc_info=True)


def _tools(hub_dir: Path, c: registry.Connector, user) -> dict:
    """The server's tools for the Console, as the connector would reach it:
    with the Shared key, with no credential, or with the administrator's own
    connection. Without one, a note says how to see them. Never raises."""
    from . import server_tools

    if c.auth_type == "shared":
        headers = registry.shared_headers(hub_dir, c.id) or {}
    elif c.auth_type == "none":
        headers = {}
    else:
        uid = getattr(user, "id", None)
        try:
            access = tokens.access_token_for(hub_dir, uid, c.id, url=c.url) if uid else None
        except Exception:  # noqa: BLE001 — the test result stands without the tools
            access = None
        if not access:
            return {"tools": None, "tools_note": "Connect your own account to see its tools."}
        headers = {"Authorization": f"Bearer {access}"}
    try:
        return {"tools": server_tools.list_tools(c.url, headers)}
    except ConnectorError as err:
        return {"tools": None, "tools_note": err.message}


def _test(hub_dir: Path, c: registry.Connector, origin: str) -> dict:
    """What the server says, for the Console. Changes nothing: no client is
    registered and no connection is touched."""
    from . import discovery
    from . import http as net

    result: dict = {"connector_id": c.id, "auth_type": c.auth_type,
                    "redirect_uri": oauth_flow.redirect_uri(origin, c.id)}
    if c.auth_type in ("none", "shared"):
        try:
            with net.client(c.url) as client:
                if c.auth_type == "shared":
                    client.headers.update(registry.shared_headers(hub_dir, c.id) or {})
                probe = discovery.probe(client, c.url)
        except ConnectorError as err:
            return {**result, "ok": False, "error": {"code": err.code, "message": err.message}}
        ok = probe.requires_auth is False
        result.update(ok=ok, requires_auth=probe.requires_auth, status=probe.status)
        if not ok:
            if probe.requires_auth and c.auth_type == "shared":
                result["error"] = {"code": "key_refused", "message": "The server refused the key."}
            elif probe.requires_auth:
                result["error"] = {"code": "requires_auth", "message": "The server asks for "
                                   "sign-in. Set authentication to OAuth."}
            else:
                result["error"] = {"code": "unexpected_status",
                                   "message": f"The server answered with status {probe.status}."}
        return result
    try:
        disc = discovery.discover(c.url)
    except ConnectorError as err:
        return {**result, "ok": False, "error": {"code": err.code, "message": err.message}}
    if c.client_id:
        registration = "pre-registered"
    elif disc.registration_endpoint:
        registration = "dynamic"
    else:
        registration = "unavailable"
    result.update(disc.summary())
    result.update(ok=registration != "unavailable", registration=registration,
                  scope=c.scopes or disc.default_scope())
    if registration == "unavailable":
        result["error"] = {"code": "registration_unavailable",
                           "message": "The server does not support dynamic client registration. "
                                      "Register Hubzoid with the provider using the redirect URI "
                                      "below, then enter the client ID."}
    return result


class RedactOAuthQuery(logging.Filter):
    """uvicorn's access log prints each request's path with its query string.
    The query of an OAuth callback (``/oauth/...``) carries a one-time code and
    the state: it is replaced before the line is written."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str)
                and args[2].startswith("/oauth/") and "?" in args[2]):
            record.args = (*args[:2], args[2].split("?", 1)[0] + "?[redacted]", *args[3:])
        return True


def redact_oauth_callback_logs() -> None:
    """Install ``RedactOAuthQuery`` on uvicorn's access log, once per process.
    The edge logs the same paths and can call this too."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactOAuthQuery) for f in access.filters):
        access.addFilter(RedactOAuthQuery())


def mount(app, hub_dir, **ctx) -> None:  # noqa: ARG001 — webapp passes runtime context
    redact_oauth_callback_logs()
    app.include_router(build_router(Path(hub_dir)))
