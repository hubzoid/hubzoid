"""Discovery and the OAuth flow against a scripted provider (in-process httpx
transport): what a real server alone cannot show. Confidential pre-registered
clients with HTTP Basic, RFC 9207 ``iss``, registration fallbacks, every
discovery refusal, a re-authorization without a refresh token, and the
discovery cache.
"""
from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from hubzoid.auth import AuthUser
from hubzoid.connectors import ConnectorError, discovery, oauth_flow, registry, tokens
from hubzoid.connectors import http as net
from tests import connectors_fakes as f

MCP = "https://mcp.example.org/mcp"
AS = "https://as.example.org"
REDIRECT_ORIGIN = "https://hub.example.org"
OWNER = AuthUser(id="local-owner", email=f.OWNER, role="admin")


class Provider:
    """An MCP server and its authorization server, scripted per test."""

    def __init__(self):
        self.prm = {"resource": MCP, "authorization_servers": [AS], "scopes_supported": ["mail.read"]}
        self.meta = {
            "issuer": AS, "authorization_endpoint": f"{AS}/authorize?tenant=t1",
            "token_endpoint": f"{AS}/token", "registration_endpoint": f"{AS}/register",
            "revocation_endpoint": f"{AS}/revoke", "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
            "authorization_response_iss_parameter_supported": True,
        }
        self.challenge = "scope=\"mail.read\""
        self.registered: list[dict] = []
        self.register_status: dict[str, int] = {}
        self.token_requests: list[dict] = []
        self.codes: dict[str, dict] = {}
        self.fetches: list[str] = []
        self.refresh_token = "rt-1"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.fetches.append(f"{request.method} {url}")
        if request.method == "POST" and url == MCP:
            return httpx.Response(401, headers={"WWW-Authenticate": (
                f'Bearer resource_metadata="https://mcp.example.org/.well-known/'
                f'oauth-protected-resource/mcp", {self.challenge}')})
        if url == "https://mcp.example.org/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json=self.prm) if self.prm else httpx.Response(404)
        if url in (f"{AS}/.well-known/oauth-authorization-server",
                   "https://mcp.example.org/.well-known/oauth-authorization-server"):
            return httpx.Response(200, json=self.meta) if self.meta else httpx.Response(404)
        if url.startswith("https://") and "/.well-known/" in url:
            return httpx.Response(404)
        if url == f"{AS}/register":
            body = json.loads(request.content)
            method = body.get("token_endpoint_auth_method")
            status = self.register_status.get(method, 201)
            if status != 201:
                return httpx.Response(status, json={"error": "invalid_client_metadata"})
            info = {**body, "client_id": f"dyn-{len(self.registered) + 1}"}
            if method != "none":
                info["client_secret"] = "dyn-secret"
            self.registered.append(info)
            return httpx.Response(201, json=info)
        if url == f"{AS}/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append({"form": form, "auth": request.headers.get("authorization")})
            grant = self.codes.pop(form.get("code", ""), None)
            if grant is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            digest = base64.urlsafe_b64encode(
                hashlib.sha256(form["code_verifier"].encode()).digest()).decode().rstrip("=")
            if digest != grant["code_challenge"] or form["redirect_uri"] != grant["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            body = {"access_token": "at-" + form["code"], "token_type": "Bearer", "expires_in": 3600}
            if self.refresh_token:
                body["refresh_token"] = self.refresh_token
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    def authorize(self, url: str, code: str = "code-1") -> dict:
        """What the provider's authorize page does after consent."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.codes[code] = q
        return {"state": q["state"], "code": code}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    f.clean_env(monkeypatch)
    return f.make_hub(tmp_path, "sales")


@pytest.fixture
def provider(monkeypatch):
    p = Provider()
    monkeypatch.setattr(net, "_transport", httpx.MockTransport(p))
    return p


def add(hub, **extra):
    return registry.create(hub, {"name": "Mail", "url": MCP, **extra}, actor="test")


def start(hub, **kw):
    return oauth_flow.start(hub, registry.get(hub, "mail"), OWNER, origin=REDIRECT_ORIGIN, **kw)


def finish(hub, answer, **kw):
    return oauth_flow.callback(hub, connector_id="mail", user=OWNER, state=answer["state"],
                               code=answer["code"], **kw)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_discovery_follows_the_challenge_and_the_metadata(provider):
    d = discovery.discover(MCP)
    assert d.probe.requires_auth is True and d.probe.scope == "mail.read"
    assert d.resource == MCP and d.issuer == AS and d.iss_supported is True
    assert d.default_scope() == "mail.read" and d.pkce == "S256"
    assert d.resource_metadata_url.endswith("/.well-known/oauth-protected-resource/mcp")
    summary = d.summary()
    assert summary["registration_endpoint"] == f"{AS}/register" and "notes" in summary


def test_the_scope_falls_back_to_the_resource_metadata(provider):
    provider.challenge = 'error="invalid_token"'
    assert discovery.discover(MCP).default_scope() == "mail.read"
    provider.prm["scopes_supported"] = None
    assert discovery.discover(MCP).default_scope() is None


@pytest.mark.parametrize("change, code", [
    ({"prm": {"resource": "https://other.example.org/mcp", "authorization_servers": [AS]}},
     "resource_mismatch"),
    ({"meta_issuer": "https://evil.example.org"}, "issuer_mismatch"),
    ({"meta_pkce": ["plain"]}, "pkce_unsupported"),
    ({"meta_token": "http://as.example.org/token"}, "invalid_url"),
    ({"no_meta": True}, "no_authorization_metadata"),
])
def test_discovery_refusals(provider, change, code):
    if "prm" in change:
        provider.prm = change["prm"]
    if "meta_issuer" in change:
        provider.meta["issuer"] = change["meta_issuer"]
    if "meta_pkce" in change:
        provider.meta["code_challenge_methods_supported"] = change["meta_pkce"]
    if "meta_token" in change:
        provider.meta["token_endpoint"] = change["meta_token"]
    if change.get("no_meta"):
        provider.meta = None
    with pytest.raises(ConnectorError) as err:
        discovery.discover(MCP)
    assert err.value.code == code and err.value.message


def test_unadvertised_pkce_is_still_s256_and_noted(provider):
    provider.meta.pop("code_challenge_methods_supported")
    d = discovery.discover(MCP)
    assert d.pkce == "assumed" and any("PKCE" in n for n in d.notes)


def test_a_server_without_resource_metadata_gets_no_resource_parameter(provider):
    provider.prm = None
    provider.meta["issuer"] = "https://mcp.example.org"
    d = discovery.discover(MCP)
    assert d.resource is None and any("resource parameter" in n for n in d.notes)


def test_urls_follow_the_https_rule():
    for bad in ("http://example.org/mcp", "https://u:p@example.org/mcp", "ftp://example.org",
                "https://example.org/mcp#x", "", None, "https:///nohost"):
        with pytest.raises(ConnectorError):
            net.check_url(bad)
    assert net.check_url(" https://example.org/mcp?x=1 ") == "https://example.org/mcp?x=1"
    assert net.check_url("http://127.0.0.1:9/mcp") == "http://127.0.0.1:9/mcp"
    # An endpoint may use loopback http only when the connector itself is on loopback.
    with pytest.raises(ConnectorError):
        net.check_url("http://127.0.0.1:9/token", base="https://example.org/mcp")
    assert net.check_url("http://127.0.0.1:9/token", base="http://localhost:8/mcp")
    # A remote server may not send Hubzoid to this machine or a metadata service.
    for bad in ("https://169.254.169.254/latest", "https://localhost/token",
                "https://127.0.0.2/token", "https://[::1]/token", "https://[::ffff:127.0.0.1]/t",
                "https://0.0.0.0/token", "https://api.localhost/token", "https://[fe80::1]/t"):
        with pytest.raises(ConnectorError) as err:
            net.check_url(bad, base="https://example.org/mcp")
        assert "local or reserved" in err.value.message, bad
    # A private network address can be an internal identity provider.
    assert net.check_url("https://10.0.0.5/token", base="https://example.org/mcp")
    assert net.check_url("https://sso.internal/token", base="https://example.org/mcp")


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
def test_a_confidential_pre_registered_client_authenticates_with_basic(hub, provider):
    add(hub, client_id="hub app", client_secret="s&cret")
    url = start(hub)
    q = parse_qs(urlsplit(url).query)
    assert q["client_id"] == ["hub app"] and q["tenant"] == ["t1"]  # the endpoint's own query kept
    assert q["resource"] == [MCP] and q["scope"] == ["mail.read"]
    finish(hub, provider.authorize(url), iss=AS)
    (req,) = provider.token_requests
    assert req["auth"] == "Basic " + base64.b64encode(b"hub%20app:s%26cret").decode()
    assert "client_secret" not in req["form"] and req["form"]["resource"] == MCP
    assert provider.registered == []
    record = tokens.token_record(hub, OWNER.id, "mail")
    assert record["client"]["auth_method"] == "client_secret_basic"


def test_client_secret_post_when_basic_is_not_offered(hub, provider):
    provider.meta["token_endpoint_auth_methods_supported"] = ["client_secret_post"]
    add(hub, client_id="app", client_secret="sec")
    url = start(hub)
    finish(hub, provider.authorize(url), iss=AS)
    (req,) = provider.token_requests
    assert req["auth"] is None and req["form"]["client_secret"] == "sec"


def test_dynamic_registration_is_cached_for_everyone(hub, provider):
    add(hub)
    start(hub)
    start(hub)
    assert len(provider.registered) == 1
    (reg,) = provider.registered
    assert reg["redirect_uris"] == [f"{REDIRECT_ORIGIN}/oauth/connectors/mail/callback"]
    assert reg["token_endpoint_auth_method"] == "none" and reg["client_name"] == "Hubzoid"
    assert reg["scope"] == "mail.read"
    # A different origin needs its own redirect URI, so its own client.
    oauth_flow.start(hub, registry.get(hub, "mail"), OWNER, origin="https://other.example.org")
    assert len(provider.registered) == 2


def test_registration_falls_back_to_a_confidential_client(hub, provider):
    provider.register_status["none"] = 400
    add(hub)
    url = start(hub)
    assert [r["token_endpoint_auth_method"] for r in provider.registered] == ["client_secret_basic"]
    finish(hub, provider.authorize(url), iss=AS)
    assert provider.token_requests[0]["auth"].startswith("Basic ")


def test_registration_refused_everywhere_says_what_to_do(hub, provider):
    provider.register_status.update({"none": 400, "client_secret_basic": 400})
    add(hub)
    with pytest.raises(ConnectorError) as err:
        start(hub)
    assert err.value.code == "registration_failed" and "client ID" in err.value.message


def test_no_registration_endpoint_needs_a_pre_registered_client(hub, provider):
    provider.meta.pop("registration_endpoint")
    add(hub)
    with pytest.raises(ConnectorError) as err:
        start(hub)
    assert err.value.code == "registration_unavailable"


# ---------------------------------------------------------------------------
# The callback
# ---------------------------------------------------------------------------
def test_iss_is_required_when_advertised_and_must_match(hub, provider):
    add(hub, client_id="app")
    for iss, code in ((None, "issuer_mismatch"), ("https://evil.example.org", "issuer_mismatch")):
        answer = provider.authorize(start(hub))
        with pytest.raises(oauth_flow.CallbackError) as err:
            finish(hub, answer, iss=iss)
        assert err.value.code == code
    assert provider.token_requests == []  # a mix-up never reaches the token endpoint
    done = finish(hub, provider.authorize(start(hub, return_to="/c/abc")), iss=AS)
    assert done.return_to == "/c/abc" and done.connector_id == "mail"


def test_a_provider_error_is_reported_not_exchanged(hub, provider):
    add(hub, client_id="app")
    answer = provider.authorize(start(hub))
    with pytest.raises(oauth_flow.CallbackError) as err:
        oauth_flow.callback(hub, connector_id="mail", user=OWNER, state=answer["state"],
                            code=None, iss=AS, error="server_error")
    assert err.value.code == "authorization_failed"


def test_a_refused_code_fails_cleanly(hub, provider):
    add(hub, client_id="app")
    answer = provider.authorize(start(hub))
    provider.codes.clear()  # the provider no longer knows the code
    with pytest.raises(oauth_flow.CallbackError) as err:
        finish(hub, answer, iss=AS)
    assert err.value.code == "token_exchange_failed"
    assert tokens.get(hub, OWNER.id, "mail") is None


def test_a_new_authorization_without_a_refresh_token_keeps_the_old_one(hub, provider):
    add(hub, client_id="app")
    finish(hub, provider.authorize(start(hub), "code-1"), iss=AS)
    assert tokens.token_record(hub, OWNER.id, "mail")["refresh_token"] == "rt-1"
    provider.refresh_token = None  # this provider issues it on the first consent only
    finish(hub, provider.authorize(start(hub), "code-2"), iss=AS)
    record = tokens.token_record(hub, OWNER.id, "mail")
    assert record["access_token"] == "at-code-2" and record["refresh_token"] == "rt-1"
    # Another client's refresh token is never borrowed.
    registry.update(hub, "mail", {"client_id": "other-app"}, actor="test")
    finish(hub, provider.authorize(start(hub), "code-3"), iss=AS)
    assert tokens.token_record(hub, OWNER.id, "mail")["refresh_token"] is None


def test_discovery_is_reused_briefly_for_repeated_connects(hub, provider):
    add(hub, client_id="app")
    start(hub)
    first = len(provider.fetches)
    start(hub)
    assert len(provider.fetches) == first  # nothing fetched again
    oauth_flow.forget_discovery()
    start(hub)
    assert len(provider.fetches) == 2 * first


# ---------------------------------------------------------------------------
# Small rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value, ok", [
    ("/account/connections", True), ("/c/abc?x=1", True), ("/portal/connect/j/done", True),
    ("//evil.example", False), ("https://evil.example/", False), ("javascript:alert(1)", False),
    ("/\\evil", False), ("", False), (None, False), ("/x\ny", False), ("/" + "a" * 1100, False),
    ("\n//evil.example", False),
])
def test_return_to_is_a_path_on_this_site(value, ok):
    assert (oauth_flow.safe_return_to(value) is not None) is ok
    assert oauth_flow.safe_return_to(" /x\n") == "/x"


def test_the_redirect_origin(monkeypatch):
    from starlette.requests import Request

    def request(host, proto=None):
        headers = [(b"host", host.encode())]
        if proto:
            headers.append((b"x-forwarded-proto", proto.encode()))
        return Request({"type": "http", "scheme": "http", "path": "/", "headers": headers,
                        "server": ("127.0.0.1", 8000), "query_string": b""})

    monkeypatch.delenv("HUBZOID_PUBLIC_URL", raising=False)
    monkeypatch.delenv("WEBUI_URL", raising=False)
    monkeypatch.delenv("HUBZOID_ALLOWED_ORIGINS", raising=False)
    assert oauth_flow.origin_for(request("localhost:3080")) == "http://localhost:3080"
    # Nothing configured: a redirect URI is never built on a Host someone sent.
    with pytest.raises(ConnectorError) as err:
        oauth_flow.origin_for(request("hub.example.org", "https"))
    assert err.value.code == "public_url_required"
    assert oauth_flow.origin_for(request("hub.example.org", "https"), strict=False) == \
        "https://hub.example.org"
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.org/b/sales")
    monkeypatch.setenv("HUBZOID_ALLOWED_ORIGINS", "https://alt.example.org")
    assert oauth_flow.origin_for(request("alt.example.org", "https")) == "https://alt.example.org"
    assert oauth_flow.origin_for(request("evil.example", "https")) == "https://hub.example.org"
    assert oauth_flow.redirect_uri("https://hub.example.org", "gmail") == \
        "https://hub.example.org/oauth/connectors/gmail/callback"


def test_a_connector_changed_during_the_exchange_revokes_the_new_tokens(hub, provider):
    add(hub, client_id="app")
    answer = provider.authorize(start(hub))
    real = provider.__call__

    def moved_meanwhile(request):
        response = real(request)
        if str(request.url) == f"{AS}/token":
            registry.update(hub, "mail", {"enabled": False}, actor="test")
        return response

    import time

    from hubzoid.connectors import http as net_

    net_._transport = httpx.MockTransport(moved_meanwhile)
    with pytest.raises(oauth_flow.CallbackError) as err:
        finish(hub, answer, iss=AS)
    assert err.value.code == "connector_changed"
    assert tokens.get(hub, OWNER.id, "mail") is None
    deadline = time.time() + 5
    while time.time() < deadline and not any(f.endswith("/revoke") for f in provider.fetches):
        time.sleep(0.05)
    assert any(f == f"POST {AS}/revoke" for f in provider.fetches)


def test_discovery_without_resource_metadata_needs_the_origin_as_issuer(provider):
    provider.prm = None
    provider.meta["issuer"] = "https://mcp.example.org/elsewhere"
    with pytest.raises(ConnectorError) as err:
        discovery.discover(MCP)
    assert err.value.code == "issuer_mismatch"
