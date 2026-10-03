"""External sign-in against a real local OpenID Connect provider
(tests/oidc_mock.py): PKCE, state and nonce, ID token checks, linking by
verified email only, domain allowlists (with Google's hosted domain), OAuth
sign-up, Microsoft's multi-tenant issuer, and safe redirects."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hubzoid.auth import oidc, routes, sessions, users
from tests.oidc_mock import MockProvider

ENV = (
    "HUBZOID_UI", "WEBUI_AUTH", "HUBZOID_PUBLIC_URL", "WEBUI_URL", "HUBZOID_ALLOWED_ORIGINS",
    "HUBZOID_ADMIN_EMAIL", "HUBZOID_ADMIN_PASSWORD", "WEBUI_ADMIN_EMAIL", "WEBUI_ADMIN_PASSWORD",
    "HUBZOID_GATEWAY_ADMIN_EMAIL", "HUBZOID_DEPLOYMENT", "DATABASE_URL", "ENABLE_SIGNUP",
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_OAUTH_SCOPE", "MICROSOFT_CLIENT_ID",
    "MICROSOFT_CLIENT_SECRET", "MICROSOFT_CLIENT_TENANT_ID", "MICROSOFT_OAUTH_SCOPE",
    "OPENID_PROVIDER_URL", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_PROVIDER_NAME",
    "OAUTH_SCOPES", "OAUTH_ALLOWED_DOMAINS", "OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "ENABLE_OAUTH_SIGNUP",
    "HUBZOID_SECRET_KEY",
)
TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"


@pytest.fixture(scope="module")
def idp():
    with MockProvider() as provider:
        yield provider


@pytest.fixture
def hub(tmp_path, monkeypatch, idp):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    monkeypatch.setenv("OPENID_PROVIDER_URL", idp.discovery_url)
    monkeypatch.setenv("OAUTH_CLIENT_ID", idp.client_id)
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", idp.client_secret)
    monkeypatch.setenv("OAUTH_PROVIDER_NAME", "Acme SSO")
    idp.claims = {"sub": "user-1", "email": "ana@example.com", "email_verified": True,
                  "name": "Ana Example"}
    idp.token_overrides = {}
    idp.discovery_issuer = idp.token_issuer = None
    idp.signing_key = None
    idp.authorize_error = None
    oidc.reset_cache()
    sessions.reset_cache()
    d = tmp_path / "sales"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: Sales\n---\nHelp.\n")
    yield d
    oidc.reset_cache()


def client(hub) -> TestClient:
    app = FastAPI()
    routes.mount(app, hub)
    return TestClient(app)


def sign_in(c, provider="oidc", redirect="/c/abc", *, tamper_state=False, drop_cookie=False):
    """Browser round trip: login redirect, the provider's authorize page, the
    callback. Returns the callback response (not followed)."""
    start = c.get(f"/oauth/{provider}/login", params={"redirect": redirect}, follow_redirects=False)
    assert start.status_code == 302, start.text
    location = start.headers["location"]
    if not location.startswith("http://127.0.0.1"):
        return start  # refused before reaching the provider
    at_provider = httpx.get(location, follow_redirects=False)
    assert at_provider.status_code == 302, at_provider.text
    back = urlsplit(at_provider.headers["location"])
    query = back.query
    if tamper_state:
        params = parse_qs(query)
        params["state"] = ["forged-state"]
        query = "&".join(f"{k}={v[0]}" for k, v in params.items())
    if drop_cookie:
        c.cookies.delete("hz_oauth", path="/oauth")
    return c.get(back.path + "?" + query, follow_redirects=False)


def error_of(response) -> str | None:
    location = response.headers.get("location", "")
    if not location.startswith("/auth?error="):
        return None
    return location.split("=", 1)[1]


def signed_in(c) -> str | None:
    user = c.get("/api/auth/session").json().get("user")
    return user["email"] if user else None


def existing(hub, email="ana@example.com", **kw):
    user = users.create(hub, email=email, name="Ana", **kw)
    users.sync_identity(hub, user)
    return user


# ---- the handshake --------------------------------------------------------------

def test_login_redirect_carries_pkce_state_and_nonce(hub, idp):
    c = client(hub)
    r = c.get("/oauth/oidc/login", params={"redirect": "/c/abc"}, follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["response_type"] == ["code"] and q["client_id"] == [idp.client_id]
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) == 43
    assert len(q["state"][0]) >= 40 and len(q["nonce"][0]) >= 40
    assert q["redirect_uri"] == ["http://testserver/oauth/oidc/callback"]
    assert q["scope"] == ["openid email profile"]
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("hz_oauth=") and "Path=/oauth" in cookie and "HttpOnly" in cookie
    assert "SameSite=lax" in cookie and "Max-Age=600" in cookie
    # The handshake is encrypted: neither state nor verifier is readable in it.
    value = cookie.split(";")[0].split("=", 1)[1]
    assert q["state"][0] not in value


def test_linking_by_verified_email_signs_in_and_redirects(hub, idp, monkeypatch):
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    user = existing(hub)
    c = client(hub)
    r = sign_in(c)
    assert r.status_code == 302 and r.headers["location"] == "/c/abc"
    assert any(v.startswith("hz_session=") for v in r.headers.get_list("set-cookie"))
    assert signed_in(c) == "ana@example.com"
    assert c.get("/api/auth/session").json()["user"]["method"] == "oidc"
    link = users.store(hub).find_identity(idp.issuer, "user-1")
    assert link["user_id"] == user["id"] and link["provider"] == "oidc"
    token_request = idp.token_requests[-1]
    assert token_request["code_verifier"] and token_request["redirect_uri"].endswith("/callback")
    # Next time the (issuer, subject) link is used, whatever the email says.
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "false")
    idp.claims = {**idp.claims, "email": "renamed@example.com", "email_verified": False}
    c2 = client(hub)
    assert sign_in(c2).headers["location"] == "/c/abc"
    assert signed_in(c2) == "ana@example.com"


def test_unverified_email_never_links(hub, idp, monkeypatch):
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    existing(hub)
    for flag in (False, "false", None):
        idp.claims = {"sub": "attacker", "email": "ana@example.com", "email_verified": flag}
        if flag is None:
            idp.claims.pop("email_verified")
        c = client(hub)
        assert error_of(sign_in(c)) == "email_not_verified"
        assert signed_in(c) is None
    assert users.store(hub).find_identity(idp.issuer, "attacker") is None


def test_without_merge_an_existing_account_is_not_linked(hub, idp):
    existing(hub)
    c = client(hub)
    assert error_of(sign_in(c)) == "not_linked"
    assert signed_in(c) is None


def test_no_account_without_oauth_signup(hub, idp):
    c = client(hub)
    assert error_of(sign_in(c)) == "no_account"
    assert users.find_by_email(hub, "ana@example.com") is None


def test_oauth_signup_creates_a_pending_account(hub, idp, monkeypatch):
    from hubzoid.access import store_for

    monkeypatch.setenv("ENABLE_OAUTH_SIGNUP", "true")
    c = client(hub)
    assert error_of(sign_in(c)) == "pending"
    user = users.find_by_email(hub, "ana@example.com")
    assert (user["status"], user["source"], user["password_enabled"]) == ("pending", "oidc", False)
    assert user["name"] == "Ana Example"
    assert store_for(hub).identity("ana@example.com")["pending"]
    users.set_status(hub, user["id"], "active")  # an administrator approves
    assert sign_in(c).headers["location"] == "/c/abc"
    assert signed_in(c) == "ana@example.com"


def test_oauth_signup_is_active_for_a_verified_allowed_domain(hub, idp, monkeypatch):
    monkeypatch.setenv("ENABLE_OAUTH_SIGNUP", "true")
    monkeypatch.setenv("OAUTH_ALLOWED_DOMAINS", "example.com, partner.example.org")
    c = client(hub)
    assert sign_in(c).headers["location"] == "/c/abc"
    assert users.find_by_email(hub, "ana@example.com")["status"] == "active"
    # Unverified: it can't vouch for an allowed domain, so nothing is created.
    idp.claims = {"sub": "u2", "email": "bo@example.com", "email_verified": False}
    assert error_of(sign_in(client(hub))) == "email_not_verified"
    assert users.find_by_email(hub, "bo@example.com") is None


def test_unverified_signup_without_domain_limits_waits_for_approval(hub, idp, monkeypatch):
    monkeypatch.setenv("ENABLE_OAUTH_SIGNUP", "true")
    idp.claims = {"sub": "u3", "email": "cy@example.com", "email_verified": False}
    assert error_of(sign_in(client(hub))) == "pending"
    assert users.find_by_email(hub, "cy@example.com")["status"] == "pending"


def test_a_linked_sign_in_with_an_unverified_email_is_judged_on_its_account(hub, idp, monkeypatch):
    """Domains restricted after linking: an unverified email claiming an
    allowed domain doesn't count; the account's own email does."""
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    existing(hub, email="ana@outside.example.net")
    idp.claims = {"sub": "lk", "email": "ana@outside.example.net", "email_verified": True}
    assert sign_in(client(hub)).headers["location"] == "/c/abc"  # linked, no limits yet
    monkeypatch.setenv("OAUTH_ALLOWED_DOMAINS", "example.com")
    idp.claims = {"sub": "lk", "email": "ana@example.com", "email_verified": False}
    assert error_of(sign_in(client(hub))) == "domain_not_allowed"
    idp.claims = {"sub": "lk", "email": "ana@example.com", "email_verified": True}
    assert sign_in(client(hub)).headers["location"] == "/c/abc"


def test_a_lost_link_race_returns_the_identitys_own_account(hub, idp, monkeypatch):
    """Two first sign-ins race to link one (issuer, subject): the account the
    identity ends up linked to is the one signed in, never the email's."""
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    winner = existing(hub, email="first@example.com")
    existing(hub, email="ana@example.com")
    st = users.store(hub)
    st.link_identity(provider="oidc", issuer=idp.issuer, subject="raced", user_id=winner["id"],
                     email="first@example.com")
    real = users.UserStore.find_identity
    calls = []

    def not_yet(self, issuer, subject):  # the first look happened before the other link landed
        calls.append(subject)
        return None if len(calls) == 1 else real(self, issuer, subject)

    monkeypatch.setattr(users.UserStore, "find_identity", not_yet)
    idp.claims = {"sub": "raced", "email": "ana@example.com", "email_verified": True}
    c = client(hub)
    assert sign_in(c).headers["location"] == "/c/abc"
    assert signed_in(c) == "first@example.com"


def test_allowed_domains_limit_every_external_sign_in(hub, idp, monkeypatch):
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    monkeypatch.setenv("ENABLE_OAUTH_SIGNUP", "true")
    monkeypatch.setenv("OAUTH_ALLOWED_DOMAINS", "partner.example.org")
    existing(hub)
    assert error_of(sign_in(client(hub))) == "domain_not_allowed"
    assert users.store(hub).find_identity(idp.issuer, "user-1") is None
    monkeypatch.setenv("OAUTH_ALLOWED_DOMAINS", "*")
    assert sign_in(client(hub)).headers["location"] == "/c/abc"


def test_sign_in_without_an_email_is_refused(hub, idp, monkeypatch):
    monkeypatch.setenv("ENABLE_OAUTH_SIGNUP", "true")
    idp.claims = {"sub": "no-mail"}
    assert error_of(sign_in(client(hub))) == "no_email"


def test_blocked_accounts_are_refused(hub, idp, monkeypatch):
    from hubzoid.access import store_for

    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    existing(hub)
    store_for(hub).suspend("ana@example.com", actor="test")
    c = client(hub)
    assert error_of(sign_in(c)) == "suspended"
    assert signed_in(c) is None


def test_the_local_owner_is_never_linked(hub, idp, monkeypatch):
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    monkeypatch.setenv("ENABLE_OAUTH_SIGNUP", "true")
    users.ensure_local_owner(hub)
    idp.claims = {"sub": "x", "email": "admin@localhost", "email_verified": True}
    assert error_of(sign_in(client(hub))) == "no_email"  # not an email with a dotted domain


# ---- the token ----------------------------------------------------------------------

@pytest.mark.parametrize("override", [
    {"nonce": "another-nonce"},
    {"aud": "someone-else"},
    {"aud": ["hubzoid-client", "other"], "azp": "other"},
    {"iss": "https://evil.example.com"},
    {"exp": 1_000_000_000},
    {"iat": 9_999_999_999},
    {"sub": None},
])
def test_bad_id_tokens_are_refused(hub, idp, monkeypatch, override):
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    existing(hub)
    idp.token_overrides = override
    c = client(hub)
    assert error_of(sign_in(c)) == "invalid_token"
    assert signed_in(c) is None


def test_a_signature_from_another_key_is_refused(hub, idp, monkeypatch):
    from joserfc.jwk import RSAKey

    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    existing(hub)
    idp.signing_key = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig"})
    assert error_of(sign_in(client(hub))) == "invalid_token"


def test_state_must_match_the_handshake(hub, idp, monkeypatch):
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    existing(hub)
    assert error_of(sign_in(client(hub), tamper_state=True)) == "state_mismatch"
    assert error_of(sign_in(client(hub), drop_cookie=True)) == "state_mismatch"
    # A callback with no handshake at all (a forged link) signs nobody in.
    c = client(hub)
    r = c.get("/oauth/oidc/callback?code=abc&state=def", follow_redirects=False)
    assert error_of(r) == "state_mismatch" and signed_in(c) is None


def test_the_handshake_is_single_use(hub, idp, monkeypatch):
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    existing(hub)
    c = client(hub)
    start = c.get("/oauth/oidc/login", follow_redirects=False)
    back = urlsplit(httpx.get(start.headers["location"], follow_redirects=False).headers["location"])
    first = c.get(back.path + "?" + back.query, follow_redirects=False)
    assert first.headers["location"] == "/"
    assert "hz_oauth=\"\"" in " ".join(first.headers.get_list("set-cookie"))
    replay = c.get(back.path + "?" + back.query, follow_redirects=False)
    assert error_of(replay) == "state_mismatch"


def test_provider_errors_and_unknown_providers(hub, idp, monkeypatch):
    idp.authorize_error = "access_denied"
    assert error_of(sign_in(client(hub))) == "access_denied"
    idp.authorize_error = "server_error"
    assert error_of(sign_in(client(hub))) == "provider_error"
    r = client(hub).get("/oauth/github/login", follow_redirects=False)
    assert error_of(r) == "not_configured"
    monkeypatch.setenv("OPENID_PROVIDER_URL", "http://idp.example.com/")  # plain http off loopback
    oidc.reset_cache()
    r = client(hub).get("/oauth/oidc/login", follow_redirects=False)
    assert error_of(r) == "provider_error"


def test_sign_in_is_off_in_local_mode(hub, monkeypatch):
    monkeypatch.delenv("HUBZOID_AUTH")
    r = client(hub).get("/oauth/oidc/login", follow_redirects=False)
    assert error_of(r) == "sign_in_off"
    assert client(hub).get("/api/auth/session").json()["providers"] == []


@pytest.mark.parametrize("redirect", ["//evil.example.com/x", "https://evil.example.com",
                                      "/\\evil.example.com", "javascript:alert(1)",
                                      # Browsers drop tabs and newlines inside a URL, so
                                      # these are //evil.example.com once decoded.
                                      "/\t/evil.example.com", "/\n/evil.example.com",
                                      "/\r/evil.example.com", "\t//evil.example.com",
                                      "/\t\\evil.example.com"])
def test_redirects_stay_on_this_site(hub, idp, monkeypatch, redirect):
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    existing(hub)
    r = sign_in(client(hub), redirect=redirect)
    assert r.headers["location"] == "/"


def test_userinfo_fills_a_missing_email(hub, idp, monkeypatch):
    """Some providers put the email only in userinfo. Its subject must match."""
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    existing(hub)
    idp.token_overrides = {"email": None, "email_verified": None}
    c = client(hub)
    assert sign_in(c).headers["location"] == "/c/abc"
    assert signed_in(c) == "ana@example.com"


# ---- callback address -----------------------------------------------------------------

def test_callback_uses_the_requests_origin_when_it_is_the_deployments(hub, idp, monkeypatch):
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.com")
    monkeypatch.setenv("HUBZOID_ALLOWED_ORIGINS", "https://chat.example.org")

    def redirect_uri(**headers):
        r = client(hub).get("/oauth/oidc/login", headers=headers, follow_redirects=False)
        return parse_qs(urlsplit(r.headers["location"]).query)["redirect_uri"][0]

    assert redirect_uri(host="chat.example.org", **{"x-forwarded-proto": "https"}) == \
        "https://chat.example.org/oauth/oidc/callback"
    assert redirect_uri(host="hub.example.com", **{"x-forwarded-proto": "https"}) == \
        "https://hub.example.com/oauth/oidc/callback"
    # Any other Host falls back to the configured public URL.
    assert redirect_uri(host="attacker.example.net") == "https://hub.example.com/oauth/oidc/callback"
    r = client(hub).get("/oauth/oidc/login", headers={"x-forwarded-proto": "https"},
                        follow_redirects=False)
    assert "Secure" in r.headers["set-cookie"]


# ---- Google and Microsoft ------------------------------------------------------------------

@pytest.fixture
def google(hub, idp, monkeypatch):
    monkeypatch.setattr(oidc, "GOOGLE_DISCOVERY", idp.discovery_url)
    monkeypatch.delenv("OPENID_PROVIDER_URL")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", idp.client_id)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", idp.client_secret)
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    return hub


def test_google_workspace_domain_is_checked_with_hd(google, idp, monkeypatch):
    monkeypatch.setenv("OAUTH_ALLOWED_DOMAINS", "example.com")
    existing(google)
    idp.claims = {"sub": "g1", "email": "ana@example.com", "email_verified": True,
                  "hd": "example.com"}
    c = client(google)
    assert sign_in(c, provider="google").headers["location"] == "/c/abc"
    assert c.get("/api/auth/session").json()["user"]["method"] == "google"
    # A personal Google account registered with the work address has no hd.
    idp.claims = {"sub": "g2", "email": "ana@example.com", "email_verified": True}
    assert error_of(sign_in(client(google), provider="google")) == "domain_not_allowed"
    idp.claims = {"sub": "g3", "email": "ana@example.com", "email_verified": True,
                  "hd": "other.example.net"}
    assert error_of(sign_in(client(google), provider="google")) == "domain_not_allowed"


def test_google_consumer_addresses_when_allowed(google, idp, monkeypatch):
    monkeypatch.setenv("OAUTH_ALLOWED_DOMAINS", "gmail.com")
    existing(google, email="someone@gmail.com")
    idp.claims = {"sub": "g4", "email": "someone@gmail.com", "email_verified": True}
    assert sign_in(client(google), provider="google").headers["location"] == "/c/abc"


def test_google_issuer_without_scheme_is_the_same_identity(google, idp):
    user = existing(google)
    idp.claims = {"sub": "g9", "email": "ana@example.com", "email_verified": True}
    idp.token_overrides = {"iss": "accounts.google.com"}
    assert sign_in(client(google), provider="google").headers["location"] == "/c/abc"
    idp.token_overrides = {}
    assert sign_in(client(google), provider="google").headers["location"] == "/c/abc"
    rows = users.store(google).identities_for(user["id"])
    assert [(r["issuer"], r["subject"]) for r in rows] == [(idp.issuer, "g9")]


def test_microsoft_multi_tenant_issuer(hub, monkeypatch):
    with MockProvider(path="/common/v2.0") as ms:
        monkeypatch.setattr(oidc, "MICROSOFT_DISCOVERY",
                            ms.origin + "/{tenant}/v2.0/.well-known/openid-configuration")
        monkeypatch.setenv("MICROSOFT_CLIENT_ID", ms.client_id)
        monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", ms.client_secret)
        monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
        ms.discovery_issuer = ms.origin + "/{tenantid}/v2.0"
        ms.token_issuer = f"{ms.origin}/{TENANT}/v2.0"
        existing(hub)
        # Microsoft sends no email_verified; the xms_edov claim vouches for it.
        ms.claims = {"sub": "m1", "tid": TENANT, "email": "ana@example.com"}
        assert error_of(sign_in(client(hub), provider="microsoft")) == "email_not_verified"
        ms.claims = {**ms.claims, "xms_edov": True}
        c = client(hub)
        assert sign_in(c, provider="microsoft").headers["location"] == "/c/abc"
        link = users.store(hub).find_identity(f"{ms.origin}/{TENANT}/v2.0", "m1")
        assert link["provider"] == "microsoft"
        # The token's tenant must be the one in its issuer.
        ms.claims = {**ms.claims, "sub": "m2", "tid": "11111111-2222-3333-4444-555555555555"}
        assert error_of(sign_in(client(hub), provider="microsoft")) == "invalid_token"


def test_provider_configuration():
    env = {"GOOGLE_CLIENT_ID": "g", "GOOGLE_CLIENT_SECRET": "s",
           "MICROSOFT_CLIENT_ID": "m", "MICROSOFT_CLIENT_SECRET": "s",
           "MICROSOFT_CLIENT_TENANT_ID": "contoso.onmicrosoft.com",
           "OPENID_PROVIDER_URL": "https://idp.example.com/realms/acme", "OAUTH_CLIENT_ID": "o",
           "OAUTH_SCOPES": "email profile groups"}
    found = {p.id: p for p in oidc.providers(env)}
    assert list(found) == ["google", "microsoft", "oidc"]
    assert found["microsoft"].discovery_url == (
        "https://login.microsoftonline.com/contoso.onmicrosoft.com/v2.0/"
        ".well-known/openid-configuration")
    assert found["oidc"].discovery_url == (
        "https://idp.example.com/realms/acme/.well-known/openid-configuration")
    assert found["oidc"].scopes == "openid email profile groups" and found["oidc"].name == "SSO"
    assert oidc.providers({"GOOGLE_CLIENT_ID": "only-an-id"}) == []
    assert oidc.providers({"MICROSOFT_CLIENT_ID": "m", "MICROSOFT_CLIENT_SECRET": "s"})[0] \
        .discovery_url.startswith("https://login.microsoftonline.com/common/")
    assert oidc.allowed_domains({}) is None
    assert oidc.allowed_domains({"OAUTH_ALLOWED_DOMAINS": "*"}) is None
    assert oidc.allowed_domains({"OAUTH_ALLOWED_DOMAINS": " A.com, b.org ,"}) == ["a.com", "b.org"]


def test_an_identity_migrated_from_open_webui_takes_its_real_issuer(hub, idp):
    """Open WebUI recorded the provider and subject, not the issuer: the
    migrated identity is found by both and upgraded at its next sign-in."""
    user = existing(hub, email="migrated@example.com")
    st = users.store(hub)
    st.link_identity(provider="oidc", issuer="openwebui-migrated:oidc", subject="user-1",
                     user_id=user["id"], email="migrated@example.com")
    c = client(hub)  # no merging by email: only the migrated identity can match
    assert sign_in(c).headers["location"] == "/c/abc"
    assert signed_in(c) == "migrated@example.com"
    assert st.find_identity("openwebui-migrated:oidc", "user-1") is None
    assert st.find_identity(idp.issuer, "user-1")["user_id"] == user["id"]
    assert sign_in(client(hub)).headers["location"] == "/c/abc"  # now by the real issuer


def test_a_migrated_identity_of_another_provider_or_a_deleted_account_is_not_used(hub, idp):
    user = existing(hub, email="other@example.com")
    st = users.store(hub)
    st.link_identity(provider="google", issuer="openwebui-migrated:google", subject="user-1",
                     user_id=user["id"], email="other@example.com")
    assert error_of(sign_in(client(hub))) == "no_account"  # oidc, not google
    gone = existing(hub, email="gone@example.com")
    st.link_identity(provider="oidc", issuer="openwebui-migrated:oidc", subject="user-1",
                     user_id=gone["id"], email="gone@example.com")
    with st.engine.begin() as conn:  # the account went away without its identity
        conn.exec_driver_sql("DELETE FROM hz_users WHERE id = ?", (gone["id"],))
    assert error_of(sign_in(client(hub))) == "no_account"
    assert st.find_identity("openwebui-migrated:oidc", "user-1") is None
