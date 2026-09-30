"""Sign-in routes: ``/api/auth/*`` and ``/oauth/{provider}/*`` (contract 6.1).

Errors are ``{"detail": {"code", "message"}}``. Request bodies are validated
here, not by FastAPI, so a refused body is never echoed back (it may hold a
password). Every state-changing route checks the same-origin rule
(``access.session.require_same_origin``), sign-in included, so another site
can neither act with a visitor's session nor sign them in to its own account.
Handlers are synchronous: FastAPI runs them in its thread pool, where password
hashing and database calls don't block the event loop.

Activity: sign-ins, failed sign-ins to an existing account, sign-outs,
password changes, set-password links used and sign-ups are recorded in the
access audit (``hz_access_audit``, organization scope), which the Console's
Activity page shows to organization administrators.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.exc import SQLAlchemyError

from .. import appmode
from . import AuthUser, current_user, links, oidc, passwords, ratelimit, require_user, users
from . import sessions as sessionlib

log = logging.getLogger("hubzoid.auth")

_NO_STORE = {"Cache-Control": "no-store"}
_LINK_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_EMAIL_MAX = 320
_SECRET_MAX = 4096


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def password_login_enabled() -> bool:
    """Password sign-in, unless ``ENABLE_LOGIN_FORM`` or ``ENABLE_PASSWORD_AUTH``
    (Open WebUI's names) is false."""
    return (_truthy(os.environ.get("ENABLE_LOGIN_FORM"), True)
            and _truthy(os.environ.get("ENABLE_PASSWORD_AUTH"), True))


def signup_enabled() -> bool:
    """Self sign-up (``ENABLE_SIGNUP``), off unless set. New accounts wait for
    an administrator's approval."""
    return _truthy(os.environ.get("ENABLE_SIGNUP"))


def error(status: int, code: str, message: str, headers: dict | None = None,
          **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra},
                         headers={**_NO_STORE, **(headers or {})})


def _rate_limited(seconds: int) -> HTTPException:
    minutes = max(1, (seconds + 59) // 60)
    return error(429, "rate_limited",
                 f"Too many attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.",
                 headers={"Retry-After": str(seconds)}, retry_after=seconds)


def _unavailable() -> HTTPException:
    return error(503, "accounts_unavailable", "Sign-in is unavailable right now. Try again shortly.")


def _same_origin(request: Request) -> None:
    from ..access.session import require_same_origin

    try:
        require_same_origin(request)
    except HTTPException:
        raise error(403, "cross_origin", "This request came from another site, so it was refused.")


def _fields(body: Any, **limits: int) -> dict[str, str]:
    """String fields from a JSON object. Names the bad fields, never values."""
    if not isinstance(body, dict):
        raise error(422, "invalid_request", "Send a JSON object.")
    bad = sorted(k for k, n in limits.items()
                 if not isinstance(body.get(k), str) or len(body[k]) > n)
    if bad:
        raise error(422, "invalid_request", "Check these fields: " + ", ".join(bad) + ".")
    return {k: body[k] for k in limits}


def _branding_name(hub_dir: Path) -> str:
    """The name the sign-in page shows: the gateway's name (``WEBUI_NAME``) or,
    for a single hub, its main agent's name, as the chat title was in 1.0.x."""
    from .. import deployment

    try:
        gateway = bool(deployment.read(hub_dir))
    except Exception:  # noqa: BLE001
        gateway = False
    name = ""
    if not gateway:
        try:
            from ..loaders.agents import load_main

            name = (load_main(hub_dir).spec.name or "").strip()
        except Exception:  # noqa: BLE001 — a broken AGENTS.md still gets a sign-in page
            name = ""
    return name or (os.environ.get("WEBUI_NAME") or "").strip() or "Hubzoid"


def _audit(hub_dir: Path, actor: str, action: str, *, subject: str | None = None,
           detail: str | None = None) -> None:
    """One Activity row (organization scope). Never raises; never a secret."""
    try:
        from ..access import store_for
        from ..access.store import ORG

        store_for(hub_dir).audit_event(actor, action, subject=subject, hub=ORG,
                                       permission=detail, surface="web")
    except Exception:  # noqa: BLE001 — the sign-in itself already happened or failed
        log.warning("auth: could not record %s in Activity", action)


def _user_view(hub_dir: Path, user: AuthUser) -> dict:
    """The signed-in person for the web app: the public fields, how this
    session signed in, and whether there is a password to change."""
    row = users.get(hub_dir, user.id) or {}
    return {**user.public(), "method": user.method,
            "password_enabled": bool(row.get("password_enabled")),
            "has_password": bool(row.get("has_password"))}


def mount(app: FastAPI, hub_dir: Path, **ctx) -> None:  # noqa: ARG001 — ctx is for other parts
    """Register the sign-in routes and create the first administrator from the
    environment when there is none yet."""
    hub_dir = Path(hub_dir)
    try:
        users.bootstrap_admin_from_env(hub_dir)
    except Exception:  # noqa: BLE001 — start anyway; sign-in shows the problem
        log.exception("auth: could not create the first administrator from the environment")
    app.include_router(build_router(hub_dir))


def build_router(hub_dir: Path) -> APIRouter:
    hub_dir = Path(hub_dir)
    router = APIRouter(tags=["auth"])

    def local_mode() -> bool:
        return not appmode.auth_enabled(hub_dir)

    def require_accounts() -> None:
        if local_mode():
            raise error(409, "sign_in_off", "Sign-in is off on this server.")

    def complete(request: Request, user: dict, *, method: str) -> JSONResponse:
        """Start a session for a verified person and answer with it."""
        from ..access import store_for

        if user["status"] != "active":
            raise error(403, "pending", "Your account is waiting for an administrator's approval.")
        users.on_sign_in(hub_dir, user)
        if store_for(hub_dir).is_suspended(user["email"]):
            raise error(403, "suspended", "This account is blocked. Ask an administrator.")
        old = sessionlib.current_token(request)
        token = sessionlib.create_session(hub_dir, user, method=method, request=request)
        if old:
            sessionlib.revoke(hub_dir, old)
        users.touch_login(hub_dir, user["id"])
        sessionlib.forget(request)
        _audit(hub_dir, user["email"], "signed_in", subject=user["email"], detail=method)
        signed_in = AuthUser(id=user["id"], email=user["email"], name=user.get("name") or "",
                             role=user["role"], method=method)
        response = JSONResponse({"user": _user_view(hub_dir, signed_in)}, headers=_NO_STORE)
        sessionlib.set_cookie(response, token, request)
        return response

    # ---- state ------------------------------------------------------------------

    @router.get("/api/auth/session")
    def session_info(request: Request):
        name = _branding_name(hub_dir)
        if local_mode():
            owner = current_user(request, hub_dir)
            body = {"authenticated": owner is not None, "mode": "local", "providers": [],
                    "password": False, "signup": False, "branding_name": name}
            if owner is not None:
                body["user"] = _user_view(hub_dir, owner)
            return JSONResponse(body, headers=_NO_STORE)
        try:
            user = current_user(request, hub_dir)
            view = _user_view(hub_dir, user) if user else None
        except SQLAlchemyError:
            raise _unavailable()
        body = {"authenticated": user is not None, "mode": "accounts",
                "providers": oidc.public_list(), "password": password_login_enabled(),
                "signup": signup_enabled(), "branding_name": name}
        if view is not None:
            body["user"] = view
        return JSONResponse(body, headers=_NO_STORE)

    # ---- password sign-in ---------------------------------------------------------

    @router.post("/api/auth/login")
    def login(request: Request, body: Any = Body(None)):
        _same_origin(request)
        require_accounts()
        if not password_login_enabled():
            raise error(403, "password_disabled",
                        "Password sign-in is off here. Use another way to sign in.")
        f = _fields(body, email=_EMAIL_MAX, password=_SECRET_MAX)
        email = users.normalize_email(f["email"])
        ip = sessionlib.client_ip(request)
        try:
            wait = ratelimit.retry_after(hub_dir, ip=ip, email=email)
            if wait:
                raise _rate_limited(wait)
            st = users.store(hub_dir)
            user = st.find_by_email(email) if email else None
            stored = st.password_hash(user["id"]) if user else None
            ok, new_hash = passwords.verify_and_update(f["password"], stored)
            if not ok:
                locked = ratelimit.record_failure(hub_dir, ip=ip, email=email)
                if user:
                    _audit(hub_dir, "sign-in", "sign_in_failed", subject=user["email"],
                           detail="password")
                if locked:
                    raise _rate_limited(locked)
                raise error(401, "invalid_credentials", "The email or password is not right.")
            if new_hash:
                st.rehash(user["id"], new_hash, stored)
            ratelimit.record_success(hub_dir, ip=ip, email=email)
            return complete(request, user, method="password")
        except SQLAlchemyError:
            log.warning("auth: sign-in failed (database error)")
            raise _unavailable()

    @router.post("/api/auth/logout", status_code=204)
    def logout(request: Request):
        _same_origin(request)
        response = Response(status_code=204, headers=_NO_STORE)
        token = sessionlib.current_token(request)
        if token and not local_mode():
            try:
                user = sessionlib.resolve_token(hub_dir, token)
                sessionlib.revoke(hub_dir, token)
            except (SQLAlchemyError, HTTPException):
                raise _unavailable()
            if user is not None:
                _audit(hub_dir, user.email, "signed_out", subject=user.email)
        sessionlib.forget(request)
        sessionlib.clear_cookie(response, request)
        return response

    @router.post("/api/auth/signup", status_code=201)
    def signup(request: Request, body: Any = Body(None)):
        _same_origin(request)
        require_accounts()
        if not signup_enabled():
            raise error(403, "signup_disabled",
                        "Sign-up is off here. Ask an administrator for an account.")
        f = _fields(body, email=_EMAIL_MAX, name=200, password=_SECRET_MAX)
        email = users.normalize_email(f["email"])
        name = f["name"].strip()
        if not users.EMAIL_PATTERN.match(email):
            raise error(422, "invalid_email", "Enter a valid email address.")
        if not name:
            raise error(422, "invalid_name", "Enter your name.")
        try:
            passwords.check(f["password"])
        except passwords.PasswordRejected as exc:
            raise error(422, "invalid_password", exc.message)
        ip = sessionlib.client_ip(request)
        try:
            wait = ratelimit.retry_after(hub_dir, ip=ip)
            if wait:
                raise _rate_limited(wait)
            try:
                user = users.store(hub_dir).create(
                    email=email, name=name, role="user", status="pending",
                    password=f["password"], source="signup")
            except users.AccountExists:
                ratelimit.record_failure(hub_dir, ip=ip)
                raise error(409, "account_exists",
                            "An account with this email already exists. Sign in instead.")
            users.sync_identity(hub_dir, user)
        except SQLAlchemyError:
            raise _unavailable()
        _audit(hub_dir, email, "signed_up", subject=email)
        return JSONResponse({"status": user["status"]}, status_code=201, headers=_NO_STORE)

    # ---- one-time links -------------------------------------------------------------

    @router.get("/api/auth/link/{token}")
    def link_info(token: str):
        try:
            info = links.inspect(hub_dir, token)
        except SQLAlchemyError:
            raise _unavailable()
        return JSONResponse(info, headers=_LINK_HEADERS)

    @router.post("/api/auth/link/{token}")
    def link_use(token: str, request: Request, body: Any = Body(None)):
        _same_origin(request)
        f = _fields(body, password=_SECRET_MAX)
        try:
            used = links.consume(hub_dir, token, password=f["password"])
        except passwords.PasswordRejected as exc:
            raise error(422, "invalid_password", exc.message)
        except links.LinkInvalid:
            raise error(410, "link_invalid", "This link has expired or was already used. "
                                             "Ask an administrator for a new one.")
        except SQLAlchemyError:
            raise _unavailable()
        _audit(hub_dir, used["email"], "password_set_with_link", subject=used["email"],
               detail=used["purpose"])
        try:
            user = users.get(hub_dir, used["user_id"])
            if user is None:
                raise error(410, "link_invalid", "This account no longer exists.")
            response = complete(request, user, method="link")
        except SQLAlchemyError:
            raise _unavailable()
        response.headers.update(_LINK_HEADERS)
        return response

    # ---- the signed-in person -----------------------------------------------------

    @router.post("/api/auth/password", status_code=204)
    def change_password(request: Request, body: Any = Body(None)):
        _same_origin(request)
        user = require_user(request, hub_dir)
        require_accounts()
        f = _fields(body, current_password=_SECRET_MAX, new_password=_SECRET_MAX)
        try:
            passwords.check(f["new_password"])
        except passwords.PasswordRejected as exc:
            raise error(422, "invalid_password", exc.message)
        ip = sessionlib.client_ip(request)
        try:
            wait = ratelimit.retry_after(hub_dir, ip=ip, email=user.email)
            if wait:
                raise _rate_limited(wait)
            st = users.store(hub_dir)
            stored = st.password_hash(user.id)
            if not stored:
                raise error(409, "no_password", "This account has no password to change. "
                                                "Sign in the way you usually do.")
            ok, _ = passwords.verify_and_update(f["current_password"], stored)
            if not ok:
                locked = ratelimit.record_failure(hub_dir, ip=ip, email=user.email)
                if locked:
                    raise _rate_limited(locked)
                raise error(401, "invalid_credentials", "Your current password is not right.")
            ratelimit.record_success(hub_dir, ip=ip, email=user.email)
            st.set_password(user.id, f["new_password"],
                            except_token_hash=sessionlib.digest(sessionlib.current_token(request)))
        except SQLAlchemyError:
            raise _unavailable()
        _audit(hub_dir, user.email, "password_changed", subject=user.email)
        return Response(status_code=204, headers=_NO_STORE)

    @router.patch("/api/auth/me")
    def update_me(request: Request, body: Any = Body(None)):
        _same_origin(request)
        user = require_user(request, hub_dir)
        f = _fields(body, name=200)
        name = f["name"].strip()
        if not name:
            raise error(422, "invalid_name", "Enter your name.")
        try:
            row = users.set_name(hub_dir, user.id, name)
            users.sync_identity(hub_dir, row)
        except KeyError:
            raise error(404, "not_found", "This account no longer exists.")
        except SQLAlchemyError:
            raise _unavailable()
        if local_mode():
            sessionlib.reset_cache()
        sessionlib.forget(request)
        renamed = AuthUser(id=user.id, email=user.email, name=name, role=user.role,
                           method=user.method)
        return JSONResponse({"user": _user_view(hub_dir, renamed)}, headers=_NO_STORE)

    # ---- external providers ----------------------------------------------------------

    def to_auth(code: str) -> RedirectResponse:
        return RedirectResponse("/auth?error=" + quote(code, safe=""), status_code=302,
                                headers=_NO_STORE)

    @router.get("/oauth/{provider_id}/login")
    def oauth_login(provider_id: str, request: Request, redirect: str | None = None):
        if local_mode():
            return to_auth("sign_in_off")
        p = oidc.provider(provider_id)
        if p is None:
            return to_auth("not_configured")
        try:
            url, handshake = oidc.start(hub_dir, p, redirect_uri=oidc.callback_url(request, p.id),
                                        return_to=oidc.safe_redirect(redirect))
        except oidc.SignInRefused as exc:
            return to_auth(exc.code)
        response = RedirectResponse(url, status_code=302, headers=_NO_STORE)
        response.set_cookie(oidc.HANDSHAKE_COOKIE, handshake, max_age=oidc.HANDSHAKE_SECONDS,
                            path=oidc.HANDSHAKE_PATH, httponly=True, samesite="lax",
                            secure=sessionlib.is_https(request))
        return response

    @router.get("/oauth/{provider_id}/callback")
    def oauth_callback(provider_id: str, request: Request, code: str | None = None,
                       state: str | None = None,
                       provider_error: str | None = Query(None, alias="error")):
        def done(response: Response) -> Response:
            response.delete_cookie(oidc.HANDSHAKE_COOKIE, path=oidc.HANDSHAKE_PATH,
                                   httponly=True, samesite="lax",
                                   secure=sessionlib.is_https(request))
            return response

        if local_mode():
            return done(to_auth("sign_in_off"))
        p = oidc.provider(provider_id)
        if p is None:
            return done(to_auth("not_configured"))
        handshake = oidc.read_handshake(hub_dir, request.cookies.get(oidc.HANDSHAKE_COOKIE), p.id)
        if provider_error:
            return done(to_auth("access_denied" if provider_error == "access_denied"
                                else "provider_error"))
        if handshake is None:
            return done(to_auth("state_mismatch"))
        try:
            claims = oidc.finish(p, handshake, code=code or "", state=state or "")
            user = oidc.account_for(hub_dir, p, claims)
            answer = complete(request, user, method=p.id)
        except oidc.SignInRefused as exc:
            if exc.code in ("invalid_token", "state_mismatch"):
                log.warning("auth: %s sign-in refused (%s)", p.id, exc)
            return done(to_auth(exc.code))
        except HTTPException as exc:
            refused = exc.detail.get("code") if isinstance(exc.detail, dict) else None
            return done(to_auth(refused or "provider_error"))
        except SQLAlchemyError:
            log.warning("auth: %s sign-in failed (database error)", p.id)
            return done(to_auth("unavailable"))
        response = RedirectResponse(oidc.safe_redirect(handshake.get("r")), status_code=302,
                                    headers=_NO_STORE)
        for value in answer.headers.getlist("set-cookie"):
            response.headers.append("set-cookie", value)
        return done(response)

    return router
