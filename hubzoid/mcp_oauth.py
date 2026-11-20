"""Hubzoid sign-in + consent for the hosted MCP surface.

FastMCP/Python MCP SDK implement the OAuth protocol endpoints. This provider owns
persistence, the account/grant policy, resource binding and browser approval.
"""

from __future__ import annotations

import asyncio
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
from sqlalchemy import text
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse
from starlette.routing import Route
from mcp.server.auth.routes import cors_middleware, build_metadata

from . import access
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
    """Resolve only current, non-pending Hubzoid accounts
    (``auth.users.mcp_account``); never trust token email."""
    from .auth.users import mcp_account

    return mcp_account(hub_dir, email=email, account_id=account_id)


def allowed(hub_dir, email):
    """May `email` use this hub over MCP: not blocked and holding `use_hub`.
    Fails closed."""
    from .access.evaluator import authorize
    from .access.store import USE_HUB

    return authorize(hub_dir, email, USE_HUB, hub=hub_dir.name).allowed


def validate_agentcore_url(value):
    """The hub's MCP server as an AgentCore MCP runtime: its invocation URL,
    exactly as AgentCore's protected resource metadata names it (it ends in
    ``/invocations?qualifier=...``, so the ``/mcp`` rule does not apply)."""
    p = urlsplit(value)
    if p.scheme != "https" or not p.netloc or p.username or p.password or p.fragment:
        raise ValueError("HUBZOID_MCP_AGENTCORE_URL must be the runtime's HTTPS invocation URL")
    return value.strip()


class HubOAuth(OAuthProvider):
    @property
    def resources(self):
        if self._find_extra is not None and time.time() >= self._next_find:
            self._next_find = time.time() + 60
            try:
                found = self._find_extra()
            except Exception:  # noqa: BLE001 - not there yet: asked again in a minute
                found = ""
            if found:
                self._resources += (validate_agentcore_url(found),)
                self._find_extra = None
        return self._resources

    def __init__(self, hub_dir, resource, *, extra_resources=(), find_extra=None):
        self.hub_dir = hub_dir
        self.resource = validate_public_url(resource)
        # Other addresses of this hub's MCP server a client may ask a token for
        # (an AgentCore MCP runtime). One issuer and one store for all of them;
        # each token's audience is the resource its grant was asked for.
        self._resources = (self.resource,) + tuple(validate_agentcore_url(r) for r in extra_resources if r)
        # () -> address or "": an AgentCore MCP runtime found by name, which may
        # be created after this process starts. Looked up until it is found.
        self._find_extra = find_extra
        self._next_find = 0.0
        # The audiences this server accepts on its own MCP endpoint: its own
        # address here; the AgentCore address inside the AgentCore MCP runtime.
        self.accept = (self.resource,)
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
        if params.resource not in self.resources:
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
            # "Revoke all credentials", a password reset, a block or a deletion
            # moved the account's credential counter: grants made before end.
            or int(who.get("auth_epoch") or 0) != int(g.get("auth_epoch") or 0)
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

    def signing(self):
        from . import mcp_signing

        return mcp_signing.ring(self.hub_dir, ACCESS_SECONDS)

    def issue(self, c, grant, scopes, resource=None):
        g = self.valid_grant(c, grant)
        if not g:
            raise TokenError("invalid_grant", "Authorization is no longer active")
        audience = resource or self.resource
        if audience not in self.resources:
            raise TokenError("invalid_grant", "This grant is for an address this hub no longer serves")
        from . import mcp_signing

        now = int(time.time())
        access_expiry = min(now + ACCESS_SECONDS, g["_expires"])
        # RFC 9068: a signed access token any verifier with our public keys can
        # check (AgentCore, a load balancer). Ids only, never an email address.
        access_token = self.signing().sign({
            "iss": self.issuer, "sub": g["account_id"], "aud": audience,
            "client_id": g["client_id"], "scope": " ".join(scopes),
            "iat": now, "exp": access_expiry, "jti": mcp_signing.new_jti(), "hz_grant": grant,
        })
        refresh_token = "hz_rt_" + secrets.token_urlsafe(32)
        payload = {"grant": grant, "client_id": g["client_id"], "scopes": scopes}
        if audience != self.resource:
            payload["resource"] = audience
        self.store.put(c, refresh_token, "refresh", payload, g["_expires"])
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=access_expiry - now,
            refresh_token=refresh_token,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(self, client, authorization_code):
        # Load (or create) the signing key before the transaction: on SQLite a
        # second writer inside it would wait on itself.
        self.signing().ready()
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
            return self.issue(c, d["grant"], d["scopes"], d.get("resource"))

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
        self.signing().ready()
        result = None
        with self.store.engine.begin() as c:
            d = self.store.get(c, refresh_token.token, "refresh")
            if d and d["client_id"] == client.client_id and set(scopes) == {SCOPE}:
                if self.store.consume(c, refresh_token.token, "refresh"):
                    result = self.issue(c, d["grant"], scopes, d.get("resource"))
                else:
                    # Commit revocation even when the exchange fails (concurrent replay).
                    self.store.revoke(c, d["grant"])
        if result is None:
            raise TokenError("invalid_grant", "Refresh token is no longer valid")
        return result

    def _access(self, token):
        """(grant id, client id, scopes, expiry) of an access token, else None.
        A JWT is checked with our keys; an opaque ``hz_at_`` token from before
        2.0.9 is looked up until it expires (at most ten minutes)."""
        if token.count(".") == 2:
            for audience in self.accept:
                claims = self.signing().verify(token, issuer=self.issuer, audience=audience)
                if claims and isinstance(claims.get("hz_grant"), str):
                    return (claims["hz_grant"], str(claims.get("client_id") or ""),
                            str(claims.get("scope") or "").split(), int(claims["exp"]), str(claims["sub"]))
            return None
        with self.store.engine.connect() as c:
            d = self.store.get(c, token, "access")
        return (d["grant"], d["client_id"], d["scopes"], d["_expires"], None) if d else None

    async def load_access_token(self, token):
        found = self._access(token)
        if not found:
            return None
        grant, client_id, scopes, expires, subject = found
        with self.store.engine.connect() as c:
            g = self.valid_grant(c, grant)
        # The signed identity must agree with the live grant: one identity, never two.
        if not g or g["client_id"] != client_id or (subject is not None and subject != g["account_id"]):
            return None
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires,
            resource=self.accept[0],
            subject=g["account_id"],
            claims={"email": g["email"], "grant_id": grant},
        )

    async def revoke_token(self, token):
        with self.store.engine.begin() as c:
            d = self.store.get(c, token.token, "refresh")
            if d:
                self.store.revoke(c, d["grant"])
                return
        found = self._access(token.token)
        if found:
            with self.store.engine.begin() as c:
                self.store.revoke(c, found[0])

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
            target = form.get("resource")
            if len(self.resources) > 1 and target in self.resources and form.get("grant_type") == "authorization_code":
                # A code is good only for the address it was asked for.
                with self.store.engine.connect() as c:
                    code = self.store.get(c, str(form.get("code") or ""), "code")
                if code and (code.get("resource") or self.resource) != target:
                    target = None
            if target not in self.resources:
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
            doc = metadata.model_dump(mode="json", exclude_none=True)
            doc["jwks_uri"] = self.issuer + "/jwks"
            return JSONResponse(doc)

        async def jwks_endpoint(request):
            try:
                keys = await asyncio.to_thread(self.signing().jwks)
            except Exception:  # noqa: BLE001 - never a stack trace on a public route
                return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
            return JSONResponse(keys, headers={"Cache-Control": "public, max-age=300"})

        async def openid_configuration(request):
            # AgentCore's JWT authorizer reads an OpenID-style discovery URL.
            # Hubzoid is an OAuth server for MCP access tokens, not an OpenID
            # provider: no ID tokens, no userinfo.
            metadata = build_metadata(
                self.base_url, None, self.client_registration_options, self.revocation_options)
            doc = metadata.model_dump(mode="json", exclude_none=True)
            doc.update(
                jwks_uri=self.issuer + "/jwks",
                token_endpoint_auth_methods_supported=["none"],
                subject_types_supported=["public"],
                id_token_signing_alg_values_supported=["RS256"],
            )
            return JSONResponse(doc)

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
        result.append(Route("/mcp/oauth/jwks", cors_middleware(jwks_endpoint, ["GET", "OPTIONS"]),
                            methods=["GET", "OPTIONS"]))
        result.append(Route("/mcp/oauth/.well-known/openid-configuration",
                            cors_middleware(openid_configuration, ["GET", "OPTIONS"]),
                            methods=["GET", "OPTIONS"]))
        return result + browser_routes(self)
