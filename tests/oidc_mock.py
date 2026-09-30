"""A real OpenID Connect provider for tests, served over HTTP by uvicorn.

It implements what Hubzoid's sign-in uses: discovery, a JWKS with an RSA key,
an authorization endpoint that redirects back with a code, a token endpoint
that checks the client, the redirect URI and the PKCE verifier and issues a
signed ID token, and a userinfo endpoint. Claims are configurable per test
(``claims``), and ``token_overrides`` bends the ID token to exercise refusals
(a wrong nonce, audience or expiry; ``None`` removes a claim).

    with MockProvider() as idp:
        idp.claims = {"sub": "u1", "email": "ana@example.com", "email_verified": True}
        ...

It listens on a free port in 3200-3299 (the ports this lane may use).
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import threading
import time
from urllib.parse import urlencode

import uvicorn
from joserfc import jwt
from joserfc.jwk import RSAKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

PORTS = range(3200, 3300)


def _free_socket() -> socket.socket:
    for port in PORTS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            sock.close()
            continue
        return sock
    raise RuntimeError("no free port in 3200-3299")


class MockProvider:
    def __init__(self, *, client_id: str = "hubzoid-client", client_secret: str = "client-secret",
                 path: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret
        self.path = path.rstrip("/")  # e.g. "/tenant/v2.0" to serve under a prefix
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig", "alg": "RS256"})
        self.claims: dict = {"sub": "user-1", "email": "ana@example.com", "email_verified": True}
        self.token_overrides: dict = {}
        self.discovery_issuer: str | None = None  # e.g. a Microsoft "{tenantid}" template
        self.token_issuer: str | None = None
        self.signing_key: RSAKey | None = None  # sign with another key (bad signature)
        self.authorize_error: str | None = None
        self.auth_methods = ["client_secret_basic", "client_secret_post"]
        self.codes: dict[str, dict] = {}
        self.access_tokens: dict[str, dict] = {}
        self.token_requests: list[dict] = []
        self.authorize_requests: list[dict] = []
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # ---- addresses ------------------------------------------------------------

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def base(self) -> str:
        return self.origin + self.path

    @property
    def issuer(self) -> str:
        return self.base

    @property
    def discovery_url(self) -> str:
        return self.base + "/.well-known/openid-configuration"

    # ---- behaviour --------------------------------------------------------------

    def metadata(self) -> dict:
        return {
            "issuer": self.discovery_issuer or self.issuer,
            "authorization_endpoint": self.base + "/authorize",
            "token_endpoint": self.base + "/token",
            "jwks_uri": self.base + "/jwks",
            "userinfo_endpoint": self.base + "/userinfo",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": self.auth_methods,
            "scopes_supported": ["openid", "email", "profile"],
        }

    def id_token(self, *, nonce: str | None, claims: dict) -> str:
        now = int(time.time())
        body = {"iss": self.token_issuer or self.issuer, "aud": self.client_id, "iat": now,
                "exp": now + 300, **claims}
        if nonce is not None:
            body["nonce"] = nonce
        for key, value in self.token_overrides.items():
            if value is None:
                body.pop(key, None)
            else:
                body[key] = value
        key = self.signing_key or self.key
        return jwt.encode({"alg": "RS256", "kid": key.kid}, body, key)

    def _client_ok(self, request: Request, form) -> bool:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("basic "):
            raw = base64.b64decode(auth[6:]).decode()
            cid, _, secret = raw.partition(":")
            from urllib.parse import unquote

            return unquote(cid) == self.client_id and unquote(secret) == self.client_secret
        return (form.get("client_id") == self.client_id
                and form.get("client_secret") == self.client_secret)

    def app(self) -> Starlette:
        async def discovery(request: Request):
            return JSONResponse(self.metadata())

        async def jwks(request: Request):
            return JSONResponse({"keys": [self.key.as_dict(private=False)]})

        async def authorize(request: Request):
            q = dict(request.query_params)
            self.authorize_requests.append(q)
            redirect = q.get("redirect_uri", "")
            if q.get("client_id") != self.client_id or not redirect:
                return JSONResponse({"error": "invalid_client"}, status_code=400)
            if self.authorize_error:
                return RedirectResponse(redirect + "?" + urlencode(
                    {"error": self.authorize_error, "state": q.get("state", "")}), status_code=302)
            if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256":
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            code = secrets.token_urlsafe(24)
            self.codes[code] = {"redirect_uri": redirect, "nonce": q.get("nonce"),
                                "challenge": q.get("code_challenge"), "claims": dict(self.claims),
                                "used": False}
            return RedirectResponse(redirect + "?" + urlencode({"code": code, "state": q.get("state", "")}),
                                    status_code=302)

        async def token(request: Request):
            form = dict(await request.form())
            self.token_requests.append({k: v for k, v in form.items() if k != "client_secret"})
            if not self._client_ok(request, form):
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            entry = self.codes.get(form.get("code", ""))
            if (form.get("grant_type") != "authorization_code" or entry is None or entry["used"]
                    or entry["redirect_uri"] != form.get("redirect_uri")):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            verifier = form.get("code_verifier", "")
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            if digest.rstrip(b"=").decode() != entry["challenge"]:
                return JSONResponse({"error": "invalid_grant",
                                     "error_description": "PKCE verification failed"}, status_code=400)
            entry["used"] = True
            access = secrets.token_urlsafe(24)
            self.access_tokens[access] = entry["claims"]
            return JSONResponse({
                "access_token": access, "token_type": "Bearer", "expires_in": 3600,
                "id_token": self.id_token(nonce=entry["nonce"], claims=entry["claims"]),
            })

        async def userinfo(request: Request):
            token = (request.headers.get("authorization") or "").partition(" ")[2]
            claims = self.access_tokens.get(token)
            if claims is None:
                return JSONResponse({"error": "invalid_token"}, status_code=401)
            return JSONResponse(claims)

        prefix = self.path
        return Starlette(routes=[
            Route(prefix + "/.well-known/openid-configuration", discovery),
            Route(prefix + "/jwks", jwks),
            Route(prefix + "/authorize", authorize),
            Route(prefix + "/token", token, methods=["POST"]),
            Route(prefix + "/userinfo", userinfo),
        ])

    # ---- lifecycle ----------------------------------------------------------------

    def start(self) -> "MockProvider":
        sock = _free_socket()
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(self.app(), log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]},
                                        daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("mock OIDC provider did not start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    def __enter__(self) -> "MockProvider":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
