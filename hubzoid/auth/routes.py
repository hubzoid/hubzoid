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

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask

from .. import appmode
from . import (AuthUser, current_user, links, oidc, passwords, ratelimit, require_admin,
               require_user, users)
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


def refusal(status: int, code: str, message: str, *, headers: dict | None = None,
            background: BackgroundTask | None = None, **extra) -> JSONResponse:
    """The same JSON as ``error``, returned rather than raised, so work such
    as an audit row can run after the response is sent."""
    return JSONResponse({"detail": {"code": code, "message": message, **extra}},
                        status_code=status, headers={**_NO_STORE, **(headers or {})},
                        background=background)


def _rate_limit_args(seconds: int) -> tuple:
    minutes = max(1, (seconds + 59) // 60)
    return (429, "rate_limited",
            f"Too many attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.")


def _rate_limited(seconds: int) -> HTTPException:
    return error(*_rate_limit_args(seconds), headers={"Retry-After": str(seconds)},
                 retry_after=seconds)


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
    """The name the sign-in page shows: the same name as ``/api/branding`` (the
    gateway's name from its manifest, or a single hub's main agent), falling back
    to ``WEBUI_NAME`` and then "Hubzoid"."""
    try:
        from ..webapp_gateway import branding_scope

        scoped = (branding_scope(Path(hub_dir), None).name or "").strip()
        if scoped:
            return scoped
    except Exception:  # noqa: BLE001 - fall back to the older rule below
        pass
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


def _console(hub_dir: Path, user: AuthUser) -> bool:
    """Whether this person can open the Console: the Console's own entry check
    (organization-wide or any agent's Manage access). False when the check
    fails, so the session still answers; the Console checks again itself."""
    try:
        from ..access.identity import normalize
        from ..access.service import AccessService, Actor

        return AccessService(hub_dir).scope(Actor(normalize(user.email), "console", "session")).any
    except Exception:  # noqa: BLE001 — only hides a menu item
        log.warning("auth: could not check Console access")
        return False


def _user_view(hub_dir: Path, user: AuthUser) -> dict:
    """The signed-in person for the web app: the public fields, how this
    session signed in, whether there is a password to change, and whether the
    Console opens for them. People who manage the deployment also see the
    Hubzoid version, in Settings."""
    from .. import __version__

    row = users.get(hub_dir, user.id) or {}
    console = _console(hub_dir, user)
    return {**user.public(), "method": user.method,
            "password_enabled": bool(row.get("password_enabled")),
            "has_password": bool(row.get("has_password")),
            "console": console, **({"version": __version__} if console else {})}


def _unsafe_network(request: Request) -> bool:
    """Sign-in is off and this server listens beyond this computer, by the
    operator's explicit opt-out (``HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK``,
    the Docker try-out): the page shows a warning that stays."""
    if not _truthy(os.environ.get("HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK")):
        return False
    server = request.scope.get("server") or ()
    host = str(server[0]).strip("[]") if server else ""
    return not appmode.is_loopback_host(host) or bool(os.environ.get("HUBZOID_IN_CONTAINER")) \
        or Path("/.dockerenv").exists()


def mount(app: FastAPI, hub_dir: Path, **ctx) -> None:  # noqa: ARG001 — ctx is for other parts
    """Register the sign-in routes and create the first administrator from the
    environment when there is none yet."""
    hub_dir = Path(hub_dir)
    from . import logredact

    logredact.install()  # set-password links and OAuth codes stay out of access logs
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

    def complete(request: Request, user: dict, *, method: str,
                 directory_mfa: bool = False) -> JSONResponse:
        """A verified first step (password, provider, link). When the person
        uses two-step sign-in, or must set it up, answer with that step (and
        its cookie) instead of a session. ``user`` is the account as read with
        the credential just checked; if it changed since, no session starts."""
        from ..access import store_for
        from . import mfa
        from . import settings as auth_settings

        if user["status"] == "disabled":
            raise error(403, "account_disabled",
                        "Your company directory turned this account off. Ask your IT team.")
        if user["status"] != "active":
            raise error(403, "pending", "Your account is waiting for an administrator's approval.")
        users.on_sign_in(hub_dir, user)
        if store_for(hub_dir).is_suspended(user["email"]):
            raise error(403, "suspended", "This account is blocked. Ask an administrator.")
        if directory_mfa:
            # The company's identity provider ran its own sign-in policy
            # (including its MFA); Hubzoid doesn't ask for a second code.
            return start_session(request, user, method=method, mfa_done=True)
        if mfa.enrolled(hub_dir, user["id"]):
            token = mfa.start_challenge(hub_dir, user, "mfa_login",
                                        {"method": method, "updated_at": user["updated_at"]})
            return _with_challenge(request, JSONResponse(
                {"state": "mfa_required", "email": user["email"]}, headers=_NO_STORE), token)
        if auth_settings.mfa_required_for(hub_dir, user["role"]):
            secret = mfa.new_secret()
            token = mfa.start_challenge(hub_dir, user, "mfa_setup",
                                        {"method": method, "updated_at": user["updated_at"],
                                         "secret": secret})
            return _with_challenge(request, JSONResponse(
                {"state": "mfa_setup", "email": user["email"], "setup": _setup_view(user, secret)},
                headers=_NO_STORE), token)
        return start_session(request, user, method=method)

    def _setup_view(user: dict, secret: str) -> dict:
        from . import mfa

        issuer = _branding_name(hub_dir) or "Hubzoid"
        uri = mfa.otpauth_uri(secret, user["email"], issuer)
        return {"qr": mfa.qr_svg(uri), "key": mfa.format_secret(secret), "uri": uri,
                "issuer": issuer}

    def _with_challenge(request: Request, response: Response, token: str | None) -> Response:
        from . import mfa

        if token is None:
            response.delete_cookie(mfa.CHALLENGE_COOKIE, path="/api", httponly=True,
                                   samesite="lax", secure=sessionlib.is_https(request))
        else:
            response.set_cookie(mfa.CHALLENGE_COOKIE, token, max_age=mfa.CHALLENGE_SECONDS,
                                path="/api", httponly=True, samesite="lax",
                                secure=sessionlib.is_https(request))
        return response

    def start_session(request: Request, user: dict, *, method: str,
                      mfa_done: bool = False) -> JSONResponse:
        old = sessionlib.current_token(request)
        try:
            token = sessionlib.create_session(hub_dir, user, method=method, request=request,
                                              expect_updated_at=user["updated_at"], mfa=mfa_done)
        except sessionlib.SessionRace:
            raise error(409, "sign_in_changed",
                        "Your account changed while you were signing in. Sign in again.")
        if old:
            sessionlib.revoke(hub_dir, old)
        users.touch_login(hub_dir, user["id"])
        sessionlib.forget(request)
        _audit(hub_dir, user["email"], "signed_in", subject=user["email"],
               detail=method + ("+code" if mfa_done else ""))
        signed_in = AuthUser(id=user["id"], email=user["email"], name=user.get("name") or "",
                             role=user["role"], method=method)
        response = JSONResponse({"state": "signed_in", "user": _user_view(hub_dir, signed_in)},
                                headers=_NO_STORE)
        sessionlib.set_cookie(response, token, request)
        if mfa_done:
            _with_challenge(request, response, None)
        return response

    # ---- state ------------------------------------------------------------------

    @router.get("/api/auth/session")
    def session_info(request: Request):
        name = _branding_name(hub_dir)
        if local_mode():
            owner = current_user(request, hub_dir)
            body = {"authenticated": owner is not None, "mode": "local", "providers": [],
                    "password": False, "signup": False, "branding_name": name,
                    "unsafe_network": _unsafe_network(request)}
            if owner is not None:
                body["user"] = _user_view(hub_dir, owner)
            return JSONResponse(body, headers=_NO_STORE)
        try:
            user = current_user(request, hub_dir)
            view = _user_view(hub_dir, user) if user else None
        except SQLAlchemyError:
            raise _unavailable()
        from .directory.routes import public_providers

        body = {"authenticated": user is not None, "mode": "accounts",
                "providers": oidc.public_list() + public_providers(hub_dir),
                "password": password_login_enabled(),
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
            # The attempt is counted before the password is checked, so a
            # burst of parallel guesses can't outrun the lock.
            wait = ratelimit.admit(hub_dir, ip=ip, email=email)
            if wait:
                raise _rate_limited(wait)
            st = users.store(hub_dir)
            user, stored = st.credentials(email)
            if user is not None and users.is_local_address(user["email"]):
                # The local owner (admin@localhost) never signs in with a
                # password: Open WebUI gave it the well-known "admin" one.
                stored = None
            ok, new_hash = passwords.verify_and_update(f["password"], stored)
            if not ok:
                locked = ratelimit.record_failure(hub_dir, ip=ip, email=email)
                # Recorded after the answer is sent: a known email takes no
                # longer to refuse than an unknown one.
                audit = (BackgroundTask(_audit, hub_dir, "sign-in", "sign_in_failed",
                                        subject=user["email"], detail="password")
                         if user else None)
                if locked:
                    return refusal(*_rate_limit_args(locked), headers={"Retry-After": str(locked)},
                                   background=audit, retry_after=locked)
                return refusal(401, "invalid_credentials", "The email or password is not right.",
                               background=audit)
            if new_hash:
                bumped = st.rehash(user["id"], new_hash, stored)
                if bumped is not None:
                    user = {**user, "updated_at": bumped}
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
            # Sign-up is counted on its own key and never refunded, before the
            # shared address counter (which a success still takes back).
            wait = ratelimit.admit_signup(hub_dir, ip=ip)
            if wait:
                raise _rate_limited(wait)
            wait = ratelimit.admit(hub_dir, ip=ip)
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
            ratelimit.record_success(hub_dir, ip=ip)
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
            # The session starts only on the version the link wrote: a reset,
            # role change or block since then moved it, so none starts.
            response = complete(request, {**user, "updated_at": used["updated_at"]},
                                method="link")
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
            st = users.store(hub_dir)
            # The version read with the hash: the change applies only if the
            # account hasn't changed (a reset, role change or block) and this
            # session hasn't ended while the current password was checked.
            stored, version = st.password_state(user.id)
            if not stored:
                raise error(409, "no_password", "This account has no password to change. "
                                                "Sign in the way you usually do.")
            wait = ratelimit.admit(hub_dir, ip=ip, email=user.email)
            if wait:
                raise _rate_limited(wait)
            ok, _ = passwords.verify_and_update(f["current_password"], stored)
            if not ok:
                locked = ratelimit.record_failure(hub_dir, ip=ip, email=user.email)
                if locked:
                    raise _rate_limited(locked)
                raise error(401, "invalid_credentials", "Your current password is not right.")
            ratelimit.record_success(hub_dir, ip=ip, email=user.email)
            st.set_password(user.id, f["new_password"],
                            except_token_hash=sessionlib.digest(sessionlib.current_token(request)),
                            expect_updated_at=version)
        except users.AccountChanged:
            raise error(409, "account_changed", "Your account changed while you were changing "
                                                "your password. Reload the page and try again.")
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

    # ---- two-step sign-in ------------------------------------------------------------

    def _challenge(request: Request, purposes: tuple[str, ...]) -> dict:
        from . import mfa

        try:
            return mfa.read_challenge(hub_dir, request.cookies.get(mfa.CHALLENGE_COOKIE), purposes)
        except mfa.ChallengeInvalid:
            raise error(410, "step_expired",
                        "This sign-in step expired. Sign in again to get a new one.")

    def _code_fields(body: Any) -> tuple[str, str]:
        if not isinstance(body, dict):
            raise error(422, "invalid_request", "Send a JSON object.")
        code = body.get("code") if isinstance(body.get("code"), str) else ""
        recovery = body.get("recovery_code") if isinstance(body.get("recovery_code"), str) else ""
        if (not code and not recovery) or len(code) > 32 or len(recovery) > 64:
            raise error(422, "invalid_request", "Enter the 6-digit code from your app.")
        return code, recovery

    def _wrong(request: Request, step: dict, email: str, recovery: bool) -> HTTPException:
        from . import mfa

        left = mfa.wrong_code(hub_dir, step["token"])
        locked = ratelimit.record_failure(hub_dir, ip=sessionlib.client_ip(request), email=email)
        if locked:
            return _rate_limited(locked)
        if left <= 0:
            return error(410, "step_expired", "Too many wrong codes. Sign in again.")
        what = "That recovery code isn't right or was already used." if recovery else \
            "That code isn't right. Check the time on your phone and try the newest code."
        return error(401, "invalid_code", what, tries_left=left)

    @router.post("/api/auth/mfa/verify")
    def mfa_verify(request: Request, body: Any = Body(None)):
        """The second step: an authenticator code or a recovery code."""
        from . import mfa

        _same_origin(request)
        require_accounts()
        code, recovery = _code_fields(body)
        step = _challenge(request, ("mfa_login",))
        try:
            user = users.get(hub_dir, step["user_id"])
            if user is None or user["status"] != "active":
                raise error(410, "step_expired", "Sign in again.")
            ip = sessionlib.client_ip(request)
            wait = ratelimit.admit(hub_dir, ip=ip, email=user["email"])
            if wait:
                raise _rate_limited(wait)
            ok = (mfa.use_recovery_code(hub_dir, user["id"], recovery) if recovery
                  else mfa.verify_code(hub_dir, user["id"], code))
            if not ok:
                raise _wrong(request, step, user["email"], bool(recovery))
            if not mfa.finish_challenge(hub_dir, step["token"]):
                raise error(410, "step_expired", "This sign-in step was already used. Sign in again.")
            ratelimit.record_success(hub_dir, ip=ip, email=user["email"])
            response = start_session(request, {**user, "updated_at": step["data"]["updated_at"]},
                                     method=step["data"].get("method") or "password", mfa_done=True)
        except SQLAlchemyError:
            raise _unavailable()
        if recovery:
            _audit(hub_dir, user["email"], "mfa_recovery_code_used", subject=user["email"])
            left = mfa.status(hub_dir, user["id"])["recovery_codes_left"]
            payload = json.loads(response.body)
            payload["recovery_codes_left"] = left
            fresh = JSONResponse(payload, headers=_NO_STORE)
            for value in response.headers.getlist("set-cookie"):
                fresh.headers.append("set-cookie", value)
            return fresh
        return response

    @router.get("/api/auth/mfa/setup")
    def mfa_setup_info(request: Request):
        """The set-up step again (after a reload): its QR code and key."""
        step = _challenge(request, ("mfa_setup",))
        user = users.get(hub_dir, step["user_id"]) or {}
        return JSONResponse({"state": "mfa_setup", "email": user.get("email", ""),
                             "setup": _setup_view(user, step["data"]["secret"]),
                             "expires_at": step["expires_at"]}, headers=_NO_STORE)

    @router.post("/api/auth/mfa/setup")
    def mfa_setup_confirm(request: Request, body: Any = Body(None)):
        """Someone who must use two-step sign-in sets it up while signing in:
        the first code saves the authenticator and the session starts. The
        recovery codes come back once."""
        from . import mfa

        _same_origin(request)
        require_accounts()
        code, _ = _code_fields(body)
        step = _challenge(request, ("mfa_setup",))
        try:
            user = users.get(hub_dir, step["user_id"])
            if user is None or user["status"] != "active":
                raise error(410, "step_expired", "Sign in again.")
            matched = mfa.matching_step(step["data"]["secret"], code)
            if matched is None:
                raise _wrong(request, step, user["email"], False)
            if not mfa.finish_challenge(hub_dir, step["token"]):
                raise error(410, "step_expired", "This sign-in step was already used. Sign in again.")
            codes = mfa.activate(hub_dir, user["id"], step["data"]["secret"], matched)
            response = start_session(request, {**user, "updated_at": step["data"]["updated_at"]},
                                     method=step["data"].get("method") or "password", mfa_done=True)
        except SQLAlchemyError:
            raise _unavailable()
        _audit(hub_dir, user["email"], "mfa_enabled", subject=user["email"])
        payload = json.loads(response.body)
        payload["recovery_codes"] = codes
        fresh = JSONResponse(payload, headers=_NO_STORE)
        for value in response.headers.getlist("set-cookie"):
            fresh.headers.append("set-cookie", value)
        return fresh

    # ---- proving a recent sign-in -----------------------------------------------------

    def need_recent(request: Request) -> None:
        if not sessionlib.recently_verified(request, hub_dir):
            raise error(403, "reauth_required",
                        "Confirm it's you to continue.", reauth=_reauth_options(request))

    def _reauth_options(request: Request) -> dict:
        from . import mfa

        user = current_user(request, hub_dir)
        if user is None:
            return {}
        row = users.get(hub_dir, user.id) or {}
        linked = sorted({i["provider"] for i in users.store(hub_dir).identities_for(user.id)})
        options = {"password": bool(row.get("has_password")), "code": mfa.enrolled(hub_dir, user.id),
                   "providers": [p for p in linked if oidc.provider(p) is not None]}
        if row.get("managed_by"):
            # A company directory account confirms with its directory.
            from .directory import store as directory_store

            p = directory_store.get_provider(hub_dir, row["managed_by"])
            if p is not None and p["enabled"]:
                options["company"] = {"id": p["id"], "name": p["name"]}
        return options

    @router.post("/api/auth/reauth")
    def reauth(request: Request, body: Any = Body(None)):
        """Confirm it's you before a sensitive change: your password (when you
        have one) and, when you use two-step sign-in, a code. A provider-only
        account confirms by signing in with its provider again
        (``/oauth/<provider>/login?reauth=1``)."""
        from . import mfa

        _same_origin(request)
        user = require_user(request, hub_dir)
        if user.via_key:
            raise error(403, "key_not_allowed", "An API key can't confirm a sign-in.")
        if local_mode():
            return JSONResponse({"verified": True}, headers=_NO_STORE)
        if not isinstance(body, dict):
            raise error(422, "invalid_request", "Send a JSON object.")
        password = body.get("password") if isinstance(body.get("password"), str) else ""
        code = body.get("code") if isinstance(body.get("code"), str) else ""
        recovery = body.get("recovery_code") if isinstance(body.get("recovery_code"), str) else ""
        options = _reauth_options(request)
        ip = sessionlib.client_ip(request)
        try:
            wait = ratelimit.admit(hub_dir, ip=ip, email=user.email)
            if wait:
                raise _rate_limited(wait)
            if not options.get("password") and not options.get("code"):
                raise error(409, "reauth_with_provider",
                            "Sign in again with your provider to confirm.", reauth=options)
            if options.get("password"):
                stored = users.store(hub_dir).password_hash(user.id)
                ok, _ = passwords.verify_and_update(password, stored)
                if not ok:
                    locked = ratelimit.record_failure(hub_dir, ip=ip, email=user.email)
                    if locked:
                        raise _rate_limited(locked)
                    raise error(401, "invalid_credentials", "Your password is not right.")
            if options.get("code"):
                ok = (mfa.use_recovery_code(hub_dir, user.id, recovery) if recovery
                      else mfa.verify_code(hub_dir, user.id, code))
                if not ok:
                    locked = ratelimit.record_failure(hub_dir, ip=ip, email=user.email)
                    if locked:
                        raise _rate_limited(locked)
                    raise error(401, "invalid_code", "That code isn't right. Try the newest one.")
            ratelimit.record_success(hub_dir, ip=ip, email=user.email)
            sessionlib.mark_verified(hub_dir, request)
        except SQLAlchemyError:
            raise _unavailable()
        return JSONResponse({"verified": True}, headers=_NO_STORE)

    # ---- your security: two-step sign-in, devices, keys ----------------------------------

    def _session_user(request: Request) -> AuthUser:
        user = require_user(request, hub_dir)
        return user

    @router.post("/api/me/identity-links/check")
    def link_check(request: Request):
        """Before adding a provider sign-in: confirm it's you (the browser then
        goes to ``/oauth/<provider>/login?link=1``, which checks again)."""
        _same_origin(request)
        user = require_user(request, hub_dir)
        if user.via_key:
            raise error(403, "key_not_allowed", "An API key can't add a sign-in.")
        need_recent(request)
        return JSONResponse({"ok": True}, headers=_NO_STORE)

    @router.get("/api/me/security")
    def my_security(request: Request):
        from . import api_keys, mfa
        from . import settings as auth_settings

        user = _session_user(request)
        try:
            row = users.get(hub_dir, user.id) or {}
            body = {
                "mode": "local" if local_mode() else "accounts",
                "mfa": mfa.status(hub_dir, user.id),
                "mfa_policy": auth_settings.mfa_policy(hub_dir),
                "mfa_required": auth_settings.mfa_required_for(hub_dir, user.role),
                "recently_verified": sessionlib.recently_verified(request, hub_dir),
                "reauth": _reauth_options(request),
                "has_password": bool(row.get("has_password")),
                "sessions": ([] if local_mode() else
                             sessionlib.list_sessions(hub_dir, user.id, sessionlib.current_token(request))),
                "api_keys": api_keys.list_for(hub_dir, user.id),
                "linked": [{"provider": i["provider"], "email": i.get("email") or ""}
                           for i in users.store(hub_dir).identities_for(user.id)],
                "providers": oidc.public_list(),
            }
        except SQLAlchemyError:
            raise _unavailable()
        return JSONResponse(body, headers=_NO_STORE)

    @router.post("/api/me/mfa/enroll")
    def mfa_enroll(request: Request):
        """Start setting up an authenticator from Settings: returns its QR code
        and key. ``/api/me/mfa/confirm`` with the first code turns it on."""
        from . import mfa

        _same_origin(request)
        user = _session_user(request)
        require_accounts()
        need_recent(request)
        row = users.get(hub_dir, user.id)
        if row is None:
            raise error(404, "not_found", "This account no longer exists.")
        secret = mfa.new_secret()
        token = mfa.start_challenge(hub_dir, row, "mfa_enroll", {"secret": secret}, seconds=900)
        response = JSONResponse({"setup": _setup_view(row, secret)}, headers=_NO_STORE)
        return _with_challenge(request, response, token)

    @router.post("/api/me/mfa/confirm")
    def mfa_confirm(request: Request, body: Any = Body(None)):
        from . import mfa

        _same_origin(request)
        user = _session_user(request)
        code, _ = _code_fields(body)
        step = _challenge(request, ("mfa_enroll",))
        if step["user_id"] != user.id:
            raise error(410, "step_expired", "Start setting up again.")
        matched = mfa.matching_step(step["data"]["secret"], code)
        if matched is None:
            raise _wrong(request, step, user.email, False)
        if not mfa.finish_challenge(hub_dir, step["token"]):
            raise error(410, "step_expired", "Start setting up again.")
        codes = mfa.activate(hub_dir, user.id, step["data"]["secret"], matched)
        sessionlib.mark_verified(hub_dir, request)
        _audit(hub_dir, user.email, "mfa_enabled", subject=user.email)
        response = JSONResponse({"recovery_codes": codes, "mfa": mfa.status(hub_dir, user.id)},
                                headers=_NO_STORE)
        return _with_challenge(request, response, None)

    @router.post("/api/me/mfa/recovery-codes")
    def mfa_new_codes(request: Request):
        from . import mfa

        _same_origin(request)
        user = _session_user(request)
        need_recent(request)
        try:
            codes = mfa.new_recovery_codes(hub_dir, user.id)
        except KeyError:
            raise error(409, "mfa_off", "Turn on two-step sign-in first.")
        _audit(hub_dir, user.email, "mfa_recovery_codes_replaced", subject=user.email)
        return JSONResponse({"recovery_codes": codes}, headers=_NO_STORE)

    @router.delete("/api/me/mfa")
    def mfa_off(request: Request):
        from . import mfa
        from . import settings as auth_settings

        _same_origin(request)
        user = _session_user(request)
        need_recent(request)
        if auth_settings.mfa_required_for(hub_dir, user.role):
            raise error(409, "mfa_required", "Two-step sign-in is required for your account, so "
                                             "it can't be turned off. You can set up a new authenticator.")
        mfa.remove(hub_dir, user.id)
        _audit(hub_dir, user.email, "mfa_disabled", subject=user.email)
        return JSONResponse({"mfa": mfa.status(hub_dir, user.id)}, headers=_NO_STORE)

    @router.get("/api/me/sessions")
    def my_sessions(request: Request):
        user = _session_user(request)
        return JSONResponse({"sessions": sessionlib.list_sessions(
            hub_dir, user.id, sessionlib.current_token(request))}, headers=_NO_STORE)

    @router.delete("/api/me/sessions/{sid}", status_code=204)
    def end_my_session(sid: str, request: Request):
        _same_origin(request)
        user = _session_user(request)
        if not sessionlib.revoke_session_id(hub_dir, user.id, sid):
            raise error(404, "not_found", "That device is already signed out.")
        _audit(hub_dir, user.email, "session_ended", subject=user.email)
        return Response(status_code=204, headers=_NO_STORE)

    @router.post("/api/me/sessions/revoke-others")
    def end_other_sessions(request: Request):
        _same_origin(request)
        user = _session_user(request)
        ended = sessionlib.revoke_user(hub_dir, user.id, except_token=sessionlib.current_token(request))
        _audit(hub_dir, user.email, "sessions_ended", subject=user.email, detail=str(ended))
        return JSONResponse({"ended": ended}, headers=_NO_STORE)

    @router.get("/api/me/api-keys")
    def my_keys(request: Request):
        from . import api_keys

        user = _session_user(request)
        return JSONResponse({"api_keys": api_keys.list_for(hub_dir, user.id),
                             "operations": list(api_keys.OPERATIONS)}, headers=_NO_STORE)

    @router.post("/api/me/api-keys", status_code=201)
    def make_key(request: Request, body: Any = Body(None)):
        from . import api_keys

        _same_origin(request)
        user = _session_user(request)
        need_recent(request)
        if not isinstance(body, dict):
            raise error(422, "invalid_request", "Send a JSON object.")
        ops = body.get("operations")
        if isinstance(body.get("preset"), str):
            ops = list(api_keys.PRESETS.get(body["preset"], ()))
        days = body.get("expires_in_days", 90)
        if days is not None and not isinstance(days, int):
            raise error(422, "invalid_request", "expires_in_days must be a whole number of days.")
        hubs = body.get("hubs")
        if hubs is not None and not (isinstance(hubs, list) and all(isinstance(h, str) for h in hubs)):
            raise error(422, "invalid_request", "hubs must be a list of agent keys.")
        try:
            details, token = api_keys.create(hub_dir, user, name=str(body.get("name") or ""),
                                             operations=ops if isinstance(ops, list) else [],
                                             hubs=hubs, expires_in_days=days)
        except api_keys.KeyRefused as exc:
            raise error(422, "invalid_key", exc.message)
        except SQLAlchemyError:
            raise _unavailable()
        _audit(hub_dir, user.email, "api_key_created", subject=user.email, detail=details["prefix"])
        return JSONResponse({"api_key": details, "key": token}, status_code=201, headers=_NO_STORE)

    @router.delete("/api/me/api-keys/{key_id}", status_code=204)
    def drop_key(key_id: str, request: Request):
        from . import api_keys

        _same_origin(request)
        user = _session_user(request)
        api_keys.revoke(hub_dir, user.id, key_id)
        _audit(hub_dir, user.email, "api_key_revoked", subject=user.email, detail=key_id[:16])
        return Response(status_code=204, headers=_NO_STORE)

    # ---- administrators ---------------------------------------------------------------

    @router.get("/api/auth/mfa/policy")
    def get_mfa_policy(request: Request):
        from . import settings as auth_settings

        require_user(request, hub_dir)
        return JSONResponse({"policy": auth_settings.mfa_policy(hub_dir),
                             "choices": list(auth_settings.MFA_POLICIES)}, headers=_NO_STORE)

    @router.put("/api/auth/mfa/policy")
    def put_mfa_policy(request: Request, body: Any = Body(None)):
        from . import settings as auth_settings

        _same_origin(request)
        user = require_admin(request, hub_dir)
        f = _fields(body, policy=16)
        before = auth_settings.mfa_policy(hub_dir)
        order = {p: i for i, p in enumerate(auth_settings.MFA_POLICIES)}
        if order.get(f["policy"], 0) < order.get(before, 0):
            need_recent(request)  # weakening the rule needs a recent sign-in
        try:
            policy = auth_settings.set_mfa_policy(hub_dir, f["policy"], actor=user.email)
        except ValueError as exc:
            raise error(422, "invalid_policy", str(exc))
        _audit(hub_dir, user.email, "mfa_policy_changed", detail=policy)
        return JSONResponse({"policy": policy}, headers=_NO_STORE)

    @router.post("/api/people/{account_id}/reset-mfa")
    def admin_reset_mfa(account_id: str, request: Request):
        """An administrator turns off someone's two-step sign-in (a lost phone).
        Their authenticator, recovery codes, sessions, keys and MCP grants end
        together; they set it up again at their next sign-in."""
        from . import mfa

        _same_origin(request)
        admin = require_admin(request, hub_dir)
        need_recent(request)
        target = users.get(hub_dir, account_id)
        if target is None or target["status"] == users.DELETED:
            raise error(404, "not_found", "This account no longer exists.")
        if target["id"] == admin.id:
            raise error(409, "self_change", "Use your own Security settings for your account.")
        st = users.store(hub_dir)
        with st.engine.begin() as conn:
            mfa.remove(hub_dir, target["id"], conn=conn)
            st._revoke(conn, target["id"], time.time(), credentials=True)  # noqa: SLF001
        _audit(hub_dir, admin.email, "mfa_reset", subject=target["email"])
        return JSONResponse({"reset": True}, headers=_NO_STORE)

    @router.post("/api/people/{account_id}/revoke-credentials")
    def admin_revoke_credentials(account_id: str, request: Request):
        """Sign someone out everywhere and end every API key and MCP grant."""
        _same_origin(request)
        admin = require_admin(request, hub_dir)
        target = users.get(hub_dir, account_id)
        if target is None or target["status"] == users.DELETED:
            raise error(404, "not_found", "This account no longer exists.")
        ended = users.store(hub_dir).revoke_credentials(target["id"])
        _audit(hub_dir, admin.email, "credentials_revoked", subject=target["email"])
        return JSONResponse({"sessions_ended": ended}, headers=_NO_STORE)

    @router.post("/api/me/revoke-credentials")
    def my_revoke_credentials(request: Request):
        _same_origin(request)
        user = _session_user(request)
        need_recent(request)
        ended = users.revoke_credentials(hub_dir, user.id, except_token=sessionlib.current_token(request))
        _audit(hub_dir, user.email, "credentials_revoked", subject=user.email)
        return JSONResponse({"sessions_ended": ended}, headers=_NO_STORE)

    # ---- company directories (SAML) ------------------------------------------------

    from .directory import routes as directory_routes

    directory_routes.add_routes(
        router, hub_dir, complete=complete, error=error, same_origin=_same_origin,
        require_admin=require_admin, need_recent=need_recent, current_user=current_user,
        audit=lambda h, actor, action, detail=None: _audit(h, actor, action, detail=detail),
        sessionlib=sessionlib)

    # ---- external providers ----------------------------------------------------------

    def to_auth(code: str) -> RedirectResponse:
        return RedirectResponse("/auth?error=" + quote(code, safe=""), status_code=302,
                                headers=_NO_STORE)

    @router.get("/oauth/{provider_id}/login")
    def oauth_login(provider_id: str, request: Request, redirect: str | None = None,
                    link: str | None = None, reauth: str | None = None):
        if local_mode():
            return to_auth("sign_in_off")
        p = oidc.provider(provider_id)
        if p is None:
            return to_auth("not_configured")
        intent, owner = "sign_in", None
        if link or reauth:
            from . import mfa

            signed_in = current_user(request, hub_dir)
            if signed_in is None or signed_in.via_key:
                return to_auth("sign_in_first")
            intent, owner = ("link" if link else "reauth"), signed_in.id
            refusal = None
            if link and not sessionlib.recently_verified(request, hub_dir):
                refusal = "confirm_first"  # a stolen session can't add its own sign-in
            elif reauth and mfa.enrolled(hub_dir, signed_in.id):
                refusal = "use_code"  # two-step accounts confirm with their code
            if refusal:
                back = oidc.safe_redirect(redirect)
                sep = "&" if "?" in back else "?"
                return RedirectResponse(f"{back}{sep}{intent}={refusal}", status_code=302,
                                        headers=_NO_STORE)
        try:
            url, handshake = oidc.start(hub_dir, p, redirect_uri=oidc.callback_url(request, p.id),
                                        return_to=oidc.safe_redirect(redirect), intent=intent,
                                        user_id=owner)
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
        back = oidc.safe_redirect(handshake.get("r"))
        intent = handshake.get("i") or "sign_in"
        if intent in ("link", "reauth"):
            signed_in = current_user(request, hub_dir)
            if signed_in is None or signed_in.id != handshake.get("a"):
                return done(to_auth("sign_in_first"))
            try:
                from . import mfa

                claims = oidc.finish(p, handshake, code=code or "", state=state or "")
                if not oidc.fresh_enough(claims, handshake):
                    outcome = "not_fresh"
                elif intent == "reauth" and mfa.enrolled(hub_dir, signed_in.id):
                    outcome = "use_code"
                elif intent == "link":
                    outcome = oidc.link_to(hub_dir, p, claims, signed_in.id)
                    _audit(hub_dir, signed_in.email, "sign_in_linked", subject=signed_in.email,
                           detail=p.id)
                elif oidc.same_person(hub_dir, claims, signed_in.id):
                    sessionlib.mark_verified(hub_dir, request)
                    outcome = "verified"
                else:
                    outcome = "not_you"
            except oidc.SignInRefused as exc:
                outcome = exc.code
            except SQLAlchemyError:
                outcome = "unavailable"
            sep = "&" if "?" in back else "?"
            return done(RedirectResponse(f"{back}{sep}{intent}={quote(outcome, safe='')}",
                                         status_code=302, headers=_NO_STORE))
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
        step = json.loads(answer.body).get("state")
        if step in ("mfa_required", "mfa_setup"):
            # The provider was the first step; the code comes next on /auth.
            target = "/auth?step=" + ("code" if step == "mfa_required" else "setup") + \
                "&redirect=" + quote(back, safe="")
        else:
            target = back
        response = RedirectResponse(target, status_code=302, headers=_NO_STORE)
        for value in answer.headers.getlist("set-cookie"):
            response.headers.append("set-cookie", value)
        return done(response)

    return router
