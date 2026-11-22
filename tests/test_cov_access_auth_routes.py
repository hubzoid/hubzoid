"""Sign-in error responses, provider-link ownership and credential revocation."""
from __future__ import annotations

import time

import pytest
from sqlalchemy.exc import OperationalError

from hubzoid.auth import mfa, oidc, routes, sessions, users
from tests.test_auth_mfa import client, hub  # noqa: F401
from tests.test_auth_routes import ORIGIN, PASSWORD, person, sign_in

P = oidc.Provider("oidc", "SSO", "client", "secret", "https://idp.example.com/discovery", "openid")


@pytest.fixture
def oauth(hub, monkeypatch):
    monkeypatch.setattr(oidc, "provider", lambda pid: P if pid == "oidc" else None)
    monkeypatch.setattr(oidc, "start", lambda *a, **kw: ("https://idp.example.com/auth", "handshake"))
    monkeypatch.setattr(oidc, "finish", lambda *a, **kw: {"iss": "https://idp.example.com", "sub": "ana",
                                                      "email": "ana@example.com"})
    return client(hub)


@pytest.mark.parametrize("intent,outcome", [("link", "linked"), ("reauth", "verified"),
    ("reauth", "not_you"), ("link", "not_fresh"), ("reauth", "use_code"),
    ("link", "linked_elsewhere"), ("link", "unavailable")])
def test_provider_callback_ownership_and_freshness(hub, oauth, monkeypatch, intent, outcome):
    u = person(hub)
    assert sign_in(oauth).status_code == 200
    handshake = {"i": intent, "a": u["id"], "r": "/settings?tab=security", "s": "state", "n": "nonce",
                 "v": "verifier", "u": "https://app/callback", "e": time.time() + 600}
    monkeypatch.setattr(oidc, "read_handshake", lambda *a: handshake)
    monkeypatch.setattr(oidc, "fresh_enough", lambda *a: outcome != "not_fresh")
    monkeypatch.setattr(oidc, "same_person", lambda *a: outcome == "verified")
    monkeypatch.setattr(mfa, "enrolled", lambda *a: outcome == "use_code")
    def link(*a):
        if outcome == "linked_elsewhere":
            raise oidc.SignInRefused("linked_elsewhere")
        if outcome == "unavailable":
            raise OperationalError("select", {}, RuntimeError("offline"))
        return "linked"
    monkeypatch.setattr(oidc, "link_to", link)
    r = oauth.get("/oauth/oidc/callback?code=code&state=state", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/settings?tab=security&" + intent + "=" + outcome
    assert "hz_oauth=" in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]


def test_provider_callback_requires_original_account(hub, oauth, monkeypatch):
    person(hub)
    assert sign_in(oauth).status_code == 200
    monkeypatch.setattr(oidc, "read_handshake", lambda *a: {"i": "link", "a": "other", "r": "/"})
    r = oauth.get("/oauth/oidc/callback", follow_redirects=False)
    assert r.headers["location"] == "/auth?error=sign_in_first"


def test_provider_login_refusals(hub, oauth, monkeypatch):
    assert oauth.get("/oauth/oidc/login?link=1", follow_redirects=False).headers["location"] == "/auth?error=sign_in_first"
    person(hub)
    assert sign_in(oauth).status_code == 200
    monkeypatch.setattr(sessions, "recently_verified", lambda *a: False)
    assert oauth.get("/oauth/oidc/login?link=1&redirect=/settings", follow_redirects=False).headers["location"] == "/settings?link=confirm_first"
    monkeypatch.setattr(mfa, "enrolled", lambda *a: True)
    assert oauth.get("/oauth/oidc/login?reauth=1&redirect=/settings", follow_redirects=False).headers["location"] == "/settings?reauth=use_code"
    def refused(*a, **kw):
        raise oidc.SignInRefused("provider_error")
    monkeypatch.setattr(oidc, "start", refused)
    assert oauth.get("/oauth/oidc/login", follow_redirects=False).headers["location"] == "/auth?error=provider_error"


@pytest.mark.parametrize("failure,code", [("token", "invalid_token"), ("db", "unavailable"), ("pending", "pending")])
def test_provider_signin_failure_is_redirected_without_secret(hub, oauth, monkeypatch, failure, code):
    u = person(hub, status="pending" if failure == "pending" else "active")
    monkeypatch.setattr(oidc, "read_handshake", lambda *a: {"r": "/"})
    def account(*a):
        if failure == "token":
            raise oidc.SignInRefused("invalid_token", "private-token")
        if failure == "db":
            raise OperationalError("select", {}, RuntimeError("private-token"))
        return u
    monkeypatch.setattr(oidc, "account_for", account)
    r = oauth.get("/oauth/oidc/callback?code=private-token&state=state", follow_redirects=False)
    assert r.headers["location"] == "/auth?error=" + code
    assert "private-token" not in r.headers["location"]


def test_provider_signin_mfa_setup_redirect(hub, oauth, monkeypatch):
    u = person(hub)
    monkeypatch.setenv("HUBZOID_MFA_REQUIRED", "all")
    monkeypatch.setattr(oidc, "read_handshake", lambda *a: {"r": "/conversation"})
    monkeypatch.setattr(oidc, "account_for", lambda *a: u)
    r = oauth.get("/oauth/oidc/callback?code=code&state=state", follow_redirects=False)
    assert r.headers["location"] == "/auth?step=setup&redirect=%2Fconversation"
    assert mfa.CHALLENGE_COOKIE in r.cookies and sessions.SESSION_COOKIE not in r.cookies


@pytest.mark.parametrize("path", ["/api/auth/mfa/verify", "/api/auth/mfa/setup"])
@pytest.mark.parametrize("body", [None, {"code": ""}, {"code": "x" * 33}, {"recovery_code": "x" * 65}])
def test_mfa_invalid_fields_are_named(hub, path, body):
    r = client(hub).post(path, headers=ORIGIN, json=body)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_request"


@pytest.mark.parametrize("method,path,patch", [("get", "/api/auth/session", "current_user"),
    ("post", "/api/auth/login", "admit")])
def test_store_failure_reports_unavailable(hub, monkeypatch, method, path, patch):
    c = client(hub)
    def broken(*a, **kw):
        raise OperationalError("select", {}, RuntimeError("private-value"))
    monkeypatch.setattr(routes if patch == "current_user" else routes.ratelimit, patch, broken)
    if method == "get":
        r = c.get(path)
    else:
        r = c.post(path, json={"email": "ana@example.com", "password": PASSWORD}, headers=ORIGIN)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "accounts_unavailable"
    assert "private-value" not in r.text


def test_session_race_does_not_start_cookie(hub, monkeypatch):
    person(hub)
    c = client(hub)
    def raced(*a, **kw):
        raise sessions.SessionRace()
    monkeypatch.setattr(sessions, "create_session", raced)
    r = sign_in(c)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "sign_in_changed"
    assert sessions.SESSION_COOKIE not in r.cookies


def test_admin_credential_revocation_keeps_account_and_rejects_missing(hub):
    person(hub, email="boss@example.com", role="admin")
    u = person(hub)
    c = client(hub)
    assert sign_in(c, email="boss@example.com").status_code == 200
    r = c.post("/api/people/" + u["id"] + "/revoke-credentials", headers=ORIGIN)
    assert r.status_code == 200 and r.json()["sessions_ended"] == 0
    assert users.epoch(hub, u["id"]) == 1
    for action in ("revoke-credentials", "reset-mfa"):
        r = c.post("/api/people/missing/" + action, headers=ORIGIN)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"


def test_branding_fallback_and_rate_message(hub, monkeypatch):
    from hubzoid import deployment, webapp_gateway
    from hubzoid.loaders import agents
    def broken(*a, **kw):
        raise ValueError("bad config")
    monkeypatch.setattr(webapp_gateway, "branding_scope", broken)
    monkeypatch.setattr(deployment, "read", broken)
    monkeypatch.setattr(agents, "load_main", broken)
    monkeypatch.setenv("WEBUI_NAME", "Fallback")
    assert routes._branding_name(hub) == "Fallback"
    assert routes._rate_limited(61).detail["message"] == "Too many attempts. Try again in 2 minutes."
