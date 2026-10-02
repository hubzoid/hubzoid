"""Open WebUI login + Hubzoid consent for the hosted MCP surface.

FastMCP/Python MCP SDK implement the OAuth protocol endpoints. This provider owns
persistence, the account/grant policy, resource binding and browser approval.
"""

from __future__ import annotations

import secrets
import re
import time
from urllib.parse import urlsplit

from fastmcp.server.auth import AccessToken, OAuthProvider
from fastmcp.server.auth.auth import TokenHandler, ClientAuthenticator
from mcp.server.auth.provider import (
    AuthorizationCode,
    RefreshToken,
    AuthorizeError,
    TokenError,
    RegistrationError,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import inspect, text
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse
from starlette.routing import Route
from mcp.server.auth.routes import cors_middleware, build_metadata

from . import access
from .access import owui_db
from .mcp_oauth_store import OAuthStore

SCOPE = "hub:access"
ACCESS_SECONDS = 600
GRANT_SECONDS = 30 * 86400


def validate_public_url(value):
    p = urlsplit(value)
    if p.scheme != "https" and not (
        p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError(
            "MCP_PUBLIC_URL requires HTTPS (HTTP allowed only on loopback)"
        )
    if (
        not p.netloc
        or p.username
        or p.password
        or p.query
        or p.fragment
        or not p.path.endswith("/mcp")
        or "%" in p.path
        or ".." in p.path
    ):
        raise ValueError(
            "MCP_PUBLIC_URL must be the canonical public endpoint ending in /mcp"
        )
    return str(AnyHttpUrl(value)).rstrip("/")


def account(hub_dir, *, email=None, account_id=None):
    """Resolve only current, non-pending accounts; never trust token email.

    Default mode: Hubzoid accounts (``auth.users.mcp_account``). Open WebUI mode:
    Open WebUI accounts, read from its database."""
    from . import appmode

    if not appmode.is_openwebui(hub_dir):
        from .auth.users import mcp_account

        return mcp_account(hub_dir, email=email, account_id=account_id)
    con = owui_db.connect_ro(hub_dir)
    if con is None:
        return None
    try:
        cols = {c["name"] for c in inspect(con).get_columns("user")}
        role = "role" if "role" in cols else "'user'"
        field, value = ("id", account_id) if account_id else ("email", email)
        row = con.execute(
            text(f'SELECT id,email,{role} FROM "user" WHERE {field}=:value'),
            {"value": value},
        ).first()
        if not row or row[2] == "pending":
            return None
        normalized = access.normalize(row[1])
        gs = access.store_for(hub_dir)
        known = gs.identity(normalized)
        if known and known.get("owui_id") and known["owui_id"] != str(row[0]):
            return None
        if gs.is_suspended(normalized):
            return None
        return {"account_id": str(row[0]), "email": normalized}
    except Exception:
        return None
    finally:
        con.close()


def allowed(hub_dir, email):
    """May `email` use this hub over MCP: not blocked and holding `use_hub`.
    Fails closed."""
    from .access.store import USE_HUB

    try:
        gs = access.store_for(hub_dir)
        if gs.is_suspended(email):
            return False
        return gs.can(email, hub_dir.name, USE_HUB)
    except Exception:
        return False


class HubOAuth(OAuthProvider):
    def __init__(self, hub_dir, resource):
        self.hub_dir = hub_dir
        self.resource = validate_public_url(resource)
        self.issuer = self.resource + "/oauth"
        self.public_path = urlsplit(self.issuer).path
        self.origin = self.resource.removesuffix(urlsplit(self.resource).path)
        self.store = OAuthStore(hub_dir, self.resource)
        super().__init__(
            base_url=self.issuer,
            resource_base_url=self.resource.removesuffix("/mcp"),
            required_scopes=[],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        )

    async def register_client(self, client_info):
        # Public clients + PKCE only. No client secret to persist or distribute.
        if client_info.token_endpoint_auth_method != "none":
            raise RegistrationError(
                "invalid_client_metadata",
                "Use public-client authentication (none) and PKCE S256",
            )
        if (
            len(client_info.redirect_uris) > 10
            or len(client_info.model_dump_json()) > 8192
        ):
            raise RegistrationError(
                "invalid_client_metadata", "Client metadata too large"
            )
        for uri in client_info.redirect_uris:
            p = urlsplit(str(uri))
            if (
                p.fragment
                or p.username
                or p.password
                or not p.hostname
                or not re.fullmatch(r"[A-Za-z0-9.:-]+", p.hostname)
                or not (
                    p.scheme == "https"
                    or (
                        p.scheme == "http"
                        and p.hostname in {"localhost", "127.0.0.1", "::1"}
                    )
                )
            ):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "HTTPS or loopback HTTP redirect required, without credentials or fragment",
                )
        with self.store.engine.begin() as c:
            self.store.cleanup(c)
            count = c.execute(
                text(
                    "SELECT count(*) FROM hz_mcp_oauth WHERE namespace=:n AND kind='client'"
                ),
                {"n": self.resource},
            ).scalar()
            if count >= 10000:
                raise RegistrationError(
                    "invalid_client_metadata",
                    "Client registration capacity reached; contact the operator",
                )
            self.store.put(
                c,
                client_info.client_id,
                "client",
                client_info.model_dump(mode="json"),
                time.time() + 365 * 86400,
            )

    async def get_client(self, client_id):
        with self.store.engine.connect() as c:
            value = self.store.get(c, client_id, "client")
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def authorize(self, client, params):
        if params.resource != self.resource:
            raise AuthorizeError(
                "invalid_request", "resource must equal the advertised MCP URL"
            )
        if set(params.scopes or [SCOPE]) != {SCOPE}:
            raise AuthorizeError("invalid_scope", "Request hub:access")
        ticket = secrets.token_urlsafe(32)
        with self.store.engine.begin() as c:
            self.store.cleanup(c)
            self.store.put(
                c,
                ticket,
                "pending",
                {
                    "client_id": client.client_id,
                    "params": params.model_dump(mode="json"),
                },
                time.time() + 600,
            )
        return self.issuer + "/consent?ticket=" + ticket

    def valid_grant(self, c, key):
        g = self.store.get(c, key, "grant")
        if not g or g["_used"]:
            return None
        who = account(self.hub_dir, account_id=g["account_id"])
        if (
            not who
            or who["email"] != g["email"]
            or not allowed(self.hub_dir, who["email"])
        ):
            return None
        return g

    async def load_authorization_code(self, client, authorization_code):
        with self.store.engine.connect() as c:
            d = self.store.get(c, authorization_code, "code")
            if (
                not d
                or d["_used"]
                or d["client_id"] != client.client_id
                or not self.valid_grant(c, d["grant"])
            ):
                return None
        return AuthorizationCode(code=authorization_code, **d)

    def issue(self, c, grant, scopes):
        g = self.valid_grant(c, grant)
        if not g:
            raise TokenError("invalid_grant", "Authorization is no longer active")
        access_token = "hz_at_" + secrets.token_urlsafe(32)
        refresh_token = "hz_rt_" + secrets.token_urlsafe(32)
        payload = {"grant": grant, "client_id": g["client_id"], "scopes": scopes}
        access_expiry = min(int(time.time()) + ACCESS_SECONDS, g["_expires"])
        self.store.put(c, access_token, "access", payload, access_expiry)
        self.store.put(c, refresh_token, "refresh", payload, g["_expires"])
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=access_expiry - int(time.time()),
            refresh_token=refresh_token,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(self, client, authorization_code):
        with self.store.engine.begin() as c:
            d = self.store.get(c, authorization_code.code, "code")
            if (
                not d
                or d["client_id"] != client.client_id
                or not self.store.consume(c, authorization_code.code, "code")
            ):
                raise TokenError(
                    "invalid_grant", "Authorization code already used or expired"
                )
            return self.issue(c, d["grant"], d["scopes"])

    async def load_refresh_token(self, client, refresh_token):
        with self.store.engine.begin() as c:
            d = self.store.get(c, refresh_token, "refresh")
            if not d or d["client_id"] != client.client_id:
                return None
            if d["_used"]:
                self.store.revoke(c, d["grant"])
                return None
            if not self.valid_grant(c, d["grant"]):
                return None
        return RefreshToken(
            token=refresh_token,
            client_id=d["client_id"],
            scopes=d["scopes"],
            expires_at=d["_expires"],
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        result = None
        with self.store.engine.begin() as c:
            d = self.store.get(c, refresh_token.token, "refresh")
            if d and d["client_id"] == client.client_id and set(scopes) == {SCOPE}:
                if self.store.consume(c, refresh_token.token, "refresh"):
                    result = self.issue(c, d["grant"], scopes)
                else:
                    # Commit revocation even when the exchange fails (concurrent replay).
                    self.store.revoke(c, d["grant"])
        if result is None:
            raise TokenError("invalid_grant", "Refresh token is no longer valid")
        return result

    async def load_access_token(self, token):
        with self.store.engine.connect() as c:
            d = self.store.get(c, token, "access")
            g = self.valid_grant(c, d["grant"]) if d else None
        if not g:
            return None
        return AccessToken(
            token=token,
            client_id=d["client_id"],
            scopes=d["scopes"],
            expires_at=d["_expires"],
            resource=self.resource,
            subject=g["account_id"],
            claims={"email": g["email"], "grant_id": d["grant"]},
        )

    async def revoke_token(self, token):
        with self.store.engine.begin() as c:
            for kind in ("access", "refresh"):
                d = self.store.get(c, token.token, kind)
                if d:
                    self.store.revoke(c, d["grant"])

    def get_routes(self, mcp_path=None):
        from .mcp_oauth_web import browser_routes

        routes = super().get_routes(mcp_path)
        result = []
        handler = TokenHandler(
            provider=self, client_authenticator=ClientAuthenticator(self)
        )

        async def token_endpoint(request):
            # SDK 1.27 validates PKCE but doesn't enforce RFC8707 on exchange.
            form = await request.form()
            if form.get("resource") != self.resource:
                return JSONResponse(
                    {
                        "error": "invalid_target",
                        "error_description": "resource must equal the advertised MCP URL",
                    },
                    status_code=400,
                    headers={"Cache-Control": "no-store"},
                )
            return await handler.handle(request)

        async def metadata_endpoint(request):
            metadata = build_metadata(
                self.base_url,
                None,
                self.client_registration_options,
                self.revocation_options,
            )
            metadata.token_endpoint_auth_methods_supported = ["none"]
            metadata.revocation_endpoint_auth_methods_supported = ["none"]
            return JSONResponse(metadata.model_dump(mode="json", exclude_none=True))

        for route in routes:
            if route.path == "/token":
                route = Route(
                    "/token",
                    cors_middleware(token_endpoint, ["POST", "OPTIONS"]),
                    methods=["POST", "OPTIONS"],
                )
            if route.path == "/.well-known/oauth-authorization-server":
                result.append(
                    Route(
                        "/.well-known/oauth-authorization-server" + self.public_path,
                        cors_middleware(metadata_endpoint, ["GET", "OPTIONS"]),
                        methods=["GET", "OPTIONS"],
                    )
                )
            elif route.path.startswith("/.well-known/"):
                result.append(route)
            else:
                result.append(
                    Route(
                        "/mcp/oauth" + route.path, route.endpoint, methods=route.methods
                    )
                )
        return result + browser_routes(self)
