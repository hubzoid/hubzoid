"""OIDC discovery, key failures, handshake validation and explicit account linking."""
from __future__ import annotations

import base64
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from hubzoid import secretbox
from hubzoid.auth import oidc, users
from tests.test_auth_mfa import hub  # noqa: F401
from tests.test_auth_routes import person

P = oidc.Provider("oidc", "SSO", "client", "secret", "https://idp.example.com/discovery", "openid email")
META = {"issuer": "https://idp.example.com", "authorization_endpoint": "https://idp.example.com/auth",
        "token_endpoint": "https://idp.example.com/token", "jwks_uri": "https://idp.example.com/keys"}


@pytest.fixture(autouse=True)
def caches():
    oidc.reset_cache()
    yield
    oidc.reset_cache()


@pytest.mark.parametrize("url", ["https://[bad", "ftp://idp.example.com", "http://idp.example.com"])
def test_insecure_provider_url_is_refused(url):
    assert not oidc._secure_url(url)
    with pytest.raises(oidc.SignInRefused) as exc:
        oidc._fetch_json(url)
    assert exc.value.code == "provider_error"


def test_invalid_microsoft_tenant_is_not_offered():
    assert oidc.providers({"MICROSOFT_CLIENT_ID": "id", "MICROSOFT_CLIENT_SECRET": "secret",
                           "MICROSOFT_CLIENT_TENANT_ID": "../wrong"}) == []


@pytest.mark.parametrize("metadata", [{}, {**META, "token_endpoint": "http://remote.example.com/token"}])
def test_bad_discovery_metadata_refuses(monkeypatch, metadata):
    monkeypatch.setattr(oidc, "_fetch_json", lambda url: metadata)
    with pytest.raises(oidc.SignInRefused) as exc:
        oidc.discovery(P)
    assert exc.value.code == "provider_error"


def test_discovery_fetch_errors_and_nonobject_json(monkeypatch):
    monkeypatch.setattr(oidc.httpx, "get", lambda *a, **kw: httpx.Response(200, json=[], request=httpx.Request("GET", P.discovery_url)))
    with pytest.raises(ValueError, match="JSON object"):
        oidc._fetch_json(P.discovery_url)
    def failed(*a):
        raise RuntimeError("provider body with secret")
    monkeypatch.setattr(oidc, "_fetch_json", failed)
    with pytest.raises(oidc.SignInRefused, match="discovery failed") as exc:
        oidc.discovery(P)
    assert exc.value.code == "provider_error" and "secret" not in str(exc.value)


def test_unusable_keyset_and_cached_fallback(monkeypatch):
    monkeypatch.setattr(oidc, "_fetch_json", lambda url: {"keys": [None, {"use": "enc"}, {"kty": "invalid"}]})
    with pytest.raises(oidc.SignInRefused) as exc:
        oidc._keyset(META["jwks_uri"])
    assert exc.value.code == "provider_error"
    sentinel = object()
    oidc._cache["k:" + META["jwks_uri"]] = (0, sentinel)
    assert oidc._keyset(META["jwks_uri"]) is sentinel


@pytest.mark.parametrize("header", ["not-json", "[]"])
def test_unreadable_token_header(header):
    encoded = base64.urlsafe_b64encode(header.encode()).decode().rstrip("=")
    with pytest.raises(oidc.SignInRefused) as exc:
        oidc._header(encoded + ".sig.payload")
    assert exc.value.code == "invalid_token"


@pytest.mark.parametrize("overrides", [{"p": "other"}, {"e": "tomorrow"}, {"e": 1}, {"v": ""}, {"u": None}])
def test_handshake_fields_must_be_valid(hub, overrides):
    raw = secretbox.encrypt_json(hub, {"p": "oidc", "e": time.time() + 60, "s": "state", "n": "nonce",
                                      "v": "verifier", "u": "https://app/callback", **overrides})
    assert oidc.read_handshake(hub, raw, "oidc") is None


def test_handshake_tampering_and_token_auth_defaults(hub):
    assert oidc.read_handshake(hub, "bad", "oidc") is None
    assert oidc.read_handshake(hub, "x" * 4097, "oidc") is None
    assert oidc._token_auth_method(replace(P, client_secret=""), META) == "none"
    assert oidc._token_auth_method(P, {"token_endpoint_auth_methods_supported": ["private_key_jwt"]}) == "client_secret_basic"
    assert not oidc.fresh_enough({"auth_time": "bad"}, {"e": time.time() + 600})


@pytest.mark.parametrize("code,state,expected", [("code", "wrong", "state_mismatch"), ("", "state", "provider_error"),
    ("x" * 4097, "state", "provider_error")])
def test_exchange_requires_state_and_bounded_code(code, state, expected):
    with pytest.raises(oidc.SignInRefused) as exc:
        oidc.finish(P, {"s": "state"}, code=code, state=state)
    assert exc.value.code == expected


@pytest.mark.parametrize("info", [[], {"sub": "other", "email": "attacker@example.com"},
    {"sub": "person", "email": "ana@example.com", "name": "Provider name"}])
def test_userinfo_cannot_change_subject_or_existing_claims(monkeypatch, info):
    monkeypatch.setattr(oidc.httpx, "get", lambda *a, **kw: httpx.Response(200, json=info, request=httpx.Request("GET", "https://idp.example.com/userinfo")))
    claims = {"sub": "person", "name": "Existing"}
    result = oidc._with_userinfo(claims, "https://idp.example.com/userinfo", "token")
    assert result["sub"] == "person" and result["name"] == "Existing"
    assert result.get("email") == ("ana@example.com" if isinstance(info, dict) and info.get("sub") == "person" else None)
    assert claims == {"sub": "person", "name": "Existing"}


def test_userinfo_error_leaves_claims(monkeypatch):
    def broken(*a, **kw):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(oidc.httpx, "get", broken)
    assert oidc._with_userinfo({"sub": "person"}, "https://idp.example.com/userinfo", "token") == {"sub": "person"}


def test_explicit_linking_is_bound_to_account(hub):
    a = person(hub)
    b = person(hub, email="ben@example.com")
    claims = {"iss": META["issuer"], "sub": "person", "email": "ana@example.com"}
    assert oidc.link_to(hub, P, claims, a["id"]) == "linked"
    assert oidc.link_to(hub, P, claims, a["id"]) == "already"
    assert oidc.same_person(hub, claims, a["id"]) and not oidc.same_person(hub, claims, b["id"])
    with pytest.raises(oidc.SignInRefused) as exc:
        oidc.link_to(hub, P, claims, b["id"])
    assert exc.value.code == "linked_elsewhere"


def test_managed_account_cannot_link_or_use_oidc(hub):
    from hubzoid.auth.directory import store
    a = store.provision(hub, "okta", user_name="ana@example.com", email="ana@example.com", name="Ana",
                        external_id=None, active=True, resource={})
    claims = {"iss": META["issuer"], "sub": "person", "email": "ana@example.com", "email_verified": True}
    for call in (lambda: oidc.link_to(hub, P, claims, a["user_id"]),
                 lambda: oidc.account_for(hub, P, claims, {"OAUTH_MERGE_ACCOUNTS_BY_EMAIL": "true"})):
        with pytest.raises(oidc.SignInRefused) as exc:
            call()
        assert exc.value.code == "managed_account"


def test_account_link_race_returns_winner_or_refuses():
    def conflict(**kw):
        raise users.AccountExists()
    fake = SimpleNamespace(link_identity=conflict, find_identity=lambda *a: {"user_id": "winner"},
                           get=lambda uid: {"id": uid})
    assert oidc._link(fake, P, "issuer", "subject", {"id": "loser"}, "ana@example.com") == {"id": "winner"}
    fake.get = lambda uid: None
    with pytest.raises(oidc.SignInRefused) as exc:
        oidc._link(fake, P, "issuer", "subject", {"id": "loser"}, "ana@example.com")
    assert exc.value.code == "unavailable"
