"""Company sign-in routes: SAML sign-in and the administrators' set-up API.

Sign-in (public, added to the sign-in router so a SAML sign-in ends like any
other, ``auth.routes.complete``):

  GET  /api/auth/saml/<provider>/metadata   Hubzoid's service-provider metadata
  GET  /api/auth/saml/<provider>/login      start (``?redirect=``, ``?reauth=1``)
  POST /api/auth/saml/<provider>/acs        the identity provider's answer

The request id travels in ``RelayState``, encrypted with the deployment key,
and on https also in a browser-bound cookie (``SameSite=None; Secure``, since
the identity provider posts back from its own site), so an answer is accepted
only by the browser that asked, once. An unsolicited answer starts a fresh
sign-in instead (at most once a minute per browser).

Set-up (administrators, recent sign-in for changes):

  GET    /api/auth/directories                    providers, values to paste, tokens, people
  POST   /api/auth/directories                    add or update a SAML provider (off until turned on)
  POST   /api/auth/directories/<id>/enable|disable
  DELETE /api/auth/directories/<id>
  POST   /api/auth/directories/<id>/scim-tokens   a SCIM token, shown once
  DELETE /api/auth/directories/<id>/scim-tokens/<token id>
"""
from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ... import appmode, secretbox
from . import saml, store

log = logging.getLogger("hubzoid.auth.saml")

BINDING_COOKIE = "hz_saml"
BOUNCE_COOKIE = "hz_saml_bounce"
_NO_STORE = {"Cache-Control": "no-store"}


def public_providers(hub_dir: Path) -> list[dict]:
    """Enabled SAML providers, for the sign-in page's buttons."""
    try:
        return [{"id": p["id"], "name": p["name"], "kind": "saml",
                 "login_url": f"/api/auth/saml/{p['id']}/login"}
                for p in store.list_providers(hub_dir, enabled_only=True)]
    except Exception:  # noqa: BLE001 - the page still offers the other ways in
        log.warning("saml: provider list unavailable")
        return []


def _base_url(request: Request) -> str:
    return (appmode.public_url() or str(request.base_url)).rstrip("/")


def add_routes(router: APIRouter, hub_dir: Path, *, complete, error, same_origin,
               require_admin, need_recent, current_user, audit, sessionlib) -> None:
    hub_dir = Path(hub_dir)

    def to_auth(code: str) -> RedirectResponse:
        return RedirectResponse("/auth?error=" + quote(code, safe=""), status_code=302,
                                headers=_NO_STORE)

    def enabled_provider(provider_id: str) -> dict | None:
        p = store.get_provider(hub_dir, provider_id)
        return p if p is not None and p["enabled"] else None

    @router.get("/api/auth/saml/{provider_id}/metadata")
    def sp_metadata(provider_id: str, request: Request):
        p = store.get_provider(hub_dir, provider_id)
        if p is None:
            raise error(404, "not_found", "No such provider.")
        ok, why = saml.available()
        if not ok:
            raise error(503, "saml_unavailable", why)
        return Response(saml.sp_metadata(_base_url(request), p), media_type="application/samlmetadata+xml")

    @router.get("/api/auth/saml/{provider_id}/login")
    def saml_login(provider_id: str, request: Request, redirect: str | None = None,
                   reauth: str | None = None, test: str | None = None):
        from .. import oidc

        if not appmode.auth_enabled(hub_dir):
            return to_auth("sign_in_off")
        p = store.get_provider(hub_dir, provider_id)
        if p is None or (not p["enabled"] and not test):
            return to_auth("not_configured")
        ok, why = saml.available()
        if not ok:
            log.error("saml: %s", why)
            return to_auth("saml_unavailable")
        intent, owner = "sign_in", ""
        if reauth or test:
            signed_in = current_user(request, hub_dir)
            if signed_in is None or signed_in.via_key:
                return to_auth("sign_in_first")
            if test and not signed_in.is_admin:
                return to_auth("not_allowed")
            intent, owner = ("reauth" if reauth else "test"), signed_in.id
        try:
            request_id, location = saml.start(_base_url(request), p, force=bool(reauth))
        except saml.SamlRefused as exc:
            return to_auth(exc.code)
        nonce = secrets.token_urlsafe(16)
        relay = secretbox.encrypt_json(hub_dir, {
            "p": provider_id, "rid": request_id, "r": oidc.safe_redirect(redirect), "i": intent,
            "a": owner, "n": nonce, "e": time.time() + saml.REQUEST_SECONDS})
        sep = "&" if "?" in location else "?"
        response = RedirectResponse(f"{location}{sep}RelayState={quote(relay, safe='')}",
                                    status_code=302, headers=_NO_STORE)
        if sessionlib.is_https(request):
            response.set_cookie(BINDING_COOKIE, nonce, max_age=saml.REQUEST_SECONDS,
                                path=f"/api/auth/saml/{provider_id}", httponly=True,
                                samesite="none", secure=True)
        return response

    @router.post("/api/auth/saml/{provider_id}/acs")
    def saml_acs(provider_id: str, request: Request, SAMLResponse: str = Form(""),  # noqa: N803
                 RelayState: str = Form("")):  # noqa: N803
        p = store.get_provider(hub_dir, provider_id)
        if p is None:
            return to_auth("not_configured")
        state = None
        try:
            state = secretbox.decrypt_json(hub_dir, RelayState) if RelayState else None
        except Exception:  # noqa: BLE001 - tampered or foreign
            state = None
        fresh = (isinstance(state, dict) and state.get("p") == provider_id
                 and float(state.get("e") or 0) > time.time())
        if fresh and sessionlib.is_https(request):
            fresh = secrets.compare_digest(request.cookies.get(BINDING_COOKIE) or "",
                                           str(state.get("n") or ""))
        if not fresh:
            # Unsolicited (started at the identity provider) or stale: start a
            # fresh sign-in from here, at most once a minute per browser.
            if request.cookies.get(BOUNCE_COOKIE) or not p["enabled"]:
                return to_auth("saml_unsolicited")
            bounce = RedirectResponse(f"/api/auth/saml/{provider_id}/login", status_code=303,
                                      headers=_NO_STORE)
            bounce.set_cookie(BOUNCE_COOKIE, "1", max_age=60, path=f"/api/auth/saml/{provider_id}",
                              httponly=True, samesite="lax", secure=sessionlib.is_https(request))
            return bounce
        try:
            result = saml.finish(_base_url(request), p, SAMLResponse, state.get("rid"))
        except saml.SamlRefused as exc:
            return to_auth("saml_" + exc.code)
        if not store.first_use(hub_dir, provider_id, result["assertion_id"], result["expires_at"]):
            return to_auth("saml_replayed")
        matches = store.for_name_id(hub_dir, provider_id, result["name_id"])
        if len(matches) != 1:
            log.info("saml: %s sign-in for an unprovisioned or ambiguous person", provider_id)
            return to_auth("not_provisioned")
        from .. import users

        account = users.get(hub_dir, matches[0]["user_id"])
        if account is None or account.get("managed_by") != provider_id:
            return to_auth("not_provisioned")
        if not matches[0]["active"] or account["status"] != "active":
            return to_auth("account_disabled")
        back = state.get("r") or "/"
        intent = state.get("i") or "sign_in"
        if intent in ("reauth", "test"):
            # The identity provider posted here from its own site, so the
            # browser didn't send the (SameSite=Lax) session cookie. Finish on
            # a first-party GET that does, with a one-use, one-minute ticket.
            ticket = secretbox.encrypt_json(hub_dir, {
                "p": provider_id, "i": intent, "a": state.get("a"), "u": account["id"],
                "email": account["email"], "r": back, "n": secrets.token_urlsafe(12),
                "e": time.time() + 60})
            return RedirectResponse(f"/api/auth/saml/{provider_id}/done?t={quote(ticket, safe='')}",
                                    status_code=303, headers=_NO_STORE)
        try:
            answer = complete(request, account, method="saml", directory_mfa=True)
        except Exception as exc:  # noqa: BLE001 - HTTPException carries the code
            code = getattr(exc, "detail", {}).get("code") if isinstance(getattr(exc, "detail", None), dict) else None
            return to_auth(code or "unavailable")
        response = RedirectResponse(back, status_code=303, headers=_NO_STORE)
        for value in answer.headers.getlist("set-cookie"):
            response.headers.append("set-cookie", value)
        response.delete_cookie(BINDING_COOKIE, path=f"/api/auth/saml/{provider_id}")
        return response

    @router.get("/api/auth/saml/{provider_id}/done")
    def saml_done(provider_id: str, request: Request, t: str = ""):
        try:
            ticket = secretbox.decrypt_json(hub_dir, t) if t else None
        except Exception:  # noqa: BLE001
            ticket = None
        if (not isinstance(ticket, dict) or ticket.get("p") != provider_id
                or float(ticket.get("e") or 0) < time.time()
                or not store.first_use(hub_dir, provider_id, "done:" + str(ticket.get("n")), time.time() + 120)):
            return to_auth("step_expired")
        signed_in = current_user(request, hub_dir)
        if signed_in is None or signed_in.id != ticket.get("a"):
            return to_auth("sign_in_first")
        back = ticket.get("r") or "/"
        sep = "&" if "?" in back else "?"
        if ticket.get("i") == "test":
            audit(hub_dir, signed_in.email, "saml_tested", detail=provider_id)
            return RedirectResponse(f"{back}{sep}saml_test=ok&as={quote(str(ticket.get('email')))}",
                                    status_code=303, headers=_NO_STORE)
        if signed_in.id != ticket.get("u"):
            return to_auth("not_you")
        sessionlib.mark_verified(hub_dir, request)
        return RedirectResponse(f"{back}{sep}reauth=verified", status_code=303, headers=_NO_STORE)

    # ---- set-up ------------------------------------------------------------------

    def _view(request: Request, p: dict) -> dict:
        return {"id": p["id"], "name": p["name"], "enabled": p["enabled"],
                "idp": {k: p["config"].get(k) for k in ("idp_entity_id", "idp_sso_url", "metadata_url")},
                "paste": saml.sp_values(_base_url(request), p["id"]),
                "scim_tokens": store.list_tokens(hub_dir, p["id"]),
                "people": [{"user_name": a["user_name"], "active": a["active"],
                            "updated_at": a["updated_at"]} for a in store.accounts_for(hub_dir, p["id"])],
                "updated_at": p["updated_at"]}

    @router.get("/api/auth/directories")
    def list_directories(request: Request):
        require_admin(request, hub_dir)
        ok, why = saml.available()
        return JSONResponse({"available": ok, "why": why,
                             "providers": [_view(request, p) for p in store.list_providers(hub_dir)]},
                            headers=_NO_STORE)

    @router.post("/api/auth/directories")
    def save_directory(request: Request, body: Any = Body(None)):
        same_origin(request)
        admin = require_admin(request, hub_dir)
        need_recent(request)
        if not isinstance(body, dict):
            raise error(422, "invalid_request", "Send a JSON object.")
        pid = str(body.get("id") or "").strip().lower()
        xml = body.get("metadata_xml") if isinstance(body.get("metadata_xml"), str) else ""
        url = body.get("metadata_url") if isinstance(body.get("metadata_url"), str) else ""
        try:
            if url and not xml:
                xml = saml.fetch_metadata(url.strip())
            summary = saml.idp_summary(xml)
            p = store.save_provider(hub_dir, provider_id=pid, name=str(body.get("name") or ""),
                                    actor=admin.email,
                                    config={"metadata_xml": xml, "metadata_url": url or None,
                                            "idp_entity_id": summary["entity_id"],
                                            "idp_sso_url": summary["sso_url"]})
        except saml.SamlRefused as exc:
            raise error(422, exc.code, str(exc))
        except store.DirectoryError as exc:
            raise error(exc.status, exc.code, exc.message)
        audit(hub_dir, admin.email, "directory_saved", detail=pid)
        return JSONResponse(_view(request, p), headers=_NO_STORE)

    @router.post("/api/auth/directories/{provider_id}/enable")
    def enable_directory(provider_id: str, request: Request):
        return toggle_directory(provider_id, "enable", request)

    @router.post("/api/auth/directories/{provider_id}/disable")
    def disable_directory(provider_id: str, request: Request):
        return toggle_directory(provider_id, "disable", request)

    def toggle_directory(provider_id: str, action: str, request: Request):
        same_origin(request)
        admin = require_admin(request, hub_dir)
        need_recent(request)
        p = store.get_provider(hub_dir, provider_id)
        if p is None:
            raise error(404, "not_found", "No such provider.")
        p = store.save_provider(hub_dir, provider_id=provider_id, name=p["name"], config=p["config"],
                                actor=admin.email, enabled=action == "enable")
        audit(hub_dir, admin.email, f"directory_{action}d", detail=provider_id)
        return JSONResponse(_view(request, p), headers=_NO_STORE)

    @router.delete("/api/auth/directories/{provider_id}", status_code=204)
    def remove_directory(provider_id: str, request: Request):
        same_origin(request)
        admin = require_admin(request, hub_dir)
        need_recent(request)
        try:
            store.delete_provider(hub_dir, provider_id)
        except store.DirectoryError as exc:
            raise error(exc.status, exc.code, exc.message)
        audit(hub_dir, admin.email, "directory_removed", detail=provider_id)
        return Response(status_code=204, headers=_NO_STORE)

    @router.post("/api/auth/directories/{provider_id}/scim-tokens", status_code=201)
    def make_scim_token(provider_id: str, request: Request, body: Any = Body(None)):
        same_origin(request)
        admin = require_admin(request, hub_dir)
        need_recent(request)
        if store.get_provider(hub_dir, provider_id) is None:
            raise error(404, "not_found", "No such provider.")
        name = (body or {}).get("name") if isinstance(body, dict) else None
        details, token = store.create_token(hub_dir, provider_id, name=str(name or "SCIM"),
                                            actor=admin.email)
        audit(hub_dir, admin.email, "scim_token_created", detail=provider_id)
        return JSONResponse({"token": token, "scim_token": details}, status_code=201, headers=_NO_STORE)

    @router.delete("/api/auth/directories/{provider_id}/scim-tokens/{token_id}", status_code=204)
    def drop_scim_token(provider_id: str, token_id: str, request: Request):
        same_origin(request)
        admin = require_admin(request, hub_dir)
        store.revoke_token(hub_dir, provider_id, token_id)
        audit(hub_dir, admin.email, "scim_token_revoked", detail=provider_id)
        return Response(status_code=204, headers=_NO_STORE)
