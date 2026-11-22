"""Directory setup, browser binding and one-use reauthentication tickets."""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from hubzoid import secretbox
from hubzoid.auth import sessions, users
from hubzoid.auth.directory import routes, saml, store
from tests.test_auth_directory import app_for
from tests.test_auth_mfa import hub  # noqa: F401
from tests.test_auth_routes import ORIGIN, person, sign_in

pytestmark = pytest.mark.skipif(not saml.available()[0], reason="pysaml2 or xmlsec1 missing")


@pytest.fixture
def client(hub, monkeypatch):
    store.save_provider(hub, provider_id="okta", name="Okta", config={}, actor="test", enabled=True)
    monkeypatch.setattr(saml, "start", lambda *a, **kw: ("request-1", "https://idp.example.com/sso?x=1"))
    monkeypatch.setattr(saml, "finish", lambda *a: {"assertion_id": "a", "expires_at": time.time() + 600,
        "name_id": "ana@example.com", "attributes": {}})
    return TestClient(app_for(hub))


def relay(client, **params):
    r = client.get("/api/auth/saml/okta/login", params=params, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://idp.example.com")
    return parse_qs(urlparse(r.headers["location"]).query)["RelayState"][0]


def test_setup_api_validation_and_lifecycle(hub, client, monkeypatch):
    person(hub, email="boss@example.com", role="admin")
    assert sign_in(client, email="boss@example.com").status_code == 200
    summary = {"entity_id": "entity", "sso_url": "https://idp.example.com/sso"}
    monkeypatch.setattr(saml, "idp_summary", lambda xml: summary)
    monkeypatch.setattr(saml, "fetch_metadata", lambda url: "metadata")
    r = client.post("/api/auth/directories", headers=ORIGIN, json={"id": " ENTRA ", "name": "Entra",
        "metadata_url": "https://idp.example.com/metadata"})
    assert r.status_code == 200 and r.json()["id"] == "entra" and not r.json()["enabled"]
    assert store.get_provider(hub, "entra")["config"]["metadata_xml"] == "metadata"
    for action, enabled in (("enable", True), ("disable", False)):
        r = client.post("/api/auth/directories/entra/" + action, headers=ORIGIN)
        assert r.status_code == 200 and r.json()["enabled"] is enabled
    for action in ("enable", "disable", "scim-tokens"):
        r = client.post("/api/auth/directories/missing/" + action, headers=ORIGIN)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
    r = client.post("/api/auth/directories/entra/scim-tokens", headers=ORIGIN)
    assert r.status_code == 201
    token = r.json()
    assert store.token_provider(hub, token["token"]) == "entra"
    assert client.delete("/api/auth/directories/entra/scim-tokens/" + token["scim_token"]["id"], headers=ORIGIN).status_code == 204
    assert store.token_provider(hub, token["token"]) is None
    assert client.delete("/api/auth/directories/entra", headers=ORIGIN).status_code == 204
    assert store.get_provider(hub, "entra") is None
    r = client.post("/api/auth/directories", json=[], headers=ORIGIN)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_request"
    r = client.post("/api/auth/directories", json={"id": "!", "name": "Invalid"}, headers=ORIGIN)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_provider"
    def invalid(xml):
        raise saml.SamlRefused("metadata_invalid", "invalid XML")
    monkeypatch.setattr(saml, "idp_summary", invalid)
    r = client.post("/api/auth/directories", json={"id": "entra", "name": "Entra"}, headers=ORIGIN)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "metadata_invalid"


@pytest.mark.parametrize("path", ["metadata", "login", "acs"])
def test_unknown_provider(client, path):
    method = client.post if path == "acs" else client.get
    r = method("/api/auth/saml/missing/" + path, follow_redirects=False)
    if path == "metadata":
        assert r.status_code == 404
    else:
        assert r.headers["location"] == "/auth?error=not_configured"


def test_unavailable_metadata_and_login(client, monkeypatch):
    monkeypatch.setattr(saml, "available", lambda: (False, "missing SDK"))
    assert client.get("/api/auth/saml/okta/metadata").status_code == 503
    r = client.get("/api/auth/saml/okta/login", follow_redirects=False)
    assert r.headers["location"] == "/auth?error=saml_unavailable"


def test_login_intents_need_the_right_account(hub, client):
    for params in ({"reauth": "1"}, {"test": "1"}):
        r = client.get("/api/auth/saml/okta/login", params=params, follow_redirects=False)
        assert r.headers["location"] == "/auth?error=sign_in_first"
    person(hub)
    assert sign_in(client).status_code == 200
    r = client.get("/api/auth/saml/okta/login?test=1", follow_redirects=False)
    assert r.headers["location"] == "/auth?error=not_allowed"


def test_https_binding_and_reauthentication_ticket(hub, client):
    account = store.provision(hub, "okta", user_name="ana@example.com", email="ana@example.com",
        name="Ana", external_id=None, active=True, resource={})
    client.base_url = "https://testserver"
    state = relay(client, redirect="/settings", reauth="")
    assert sessions.SESSION_COOKIE not in client.cookies
    assert "hz_saml" in client.cookies
    client.cookies.delete("hz_saml")
    bounced = client.post("/api/auth/saml/okta/acs", data={"RelayState": state, "SAMLResponse": "answer"}, follow_redirects=False)
    assert bounced.headers["location"] == "/api/auth/saml/okta/login"
    # Reauth comes back through a first-party ticket tied to the same account.
    user = users.store(hub).get(account["user_id"])
    client.cookies.set(sessions.SESSION_COOKIE, sessions.create_session(hub, user, method="saml"))
    state = relay(client, redirect="/settings?tab=security", reauth="1")
    r = client.post("/api/auth/saml/okta/acs", data={"RelayState": state, "SAMLResponse": "answer"}, follow_redirects=False)
    assert r.status_code == 303 and "/done?t=" in r.headers["location"]
    ticket_url = r.headers["location"]
    done = client.get(ticket_url, follow_redirects=False)
    assert done.headers["location"] == "/settings?tab=security&reauth=verified"
    assert client.get(ticket_url, follow_redirects=False).headers["location"] == "/auth?error=step_expired"


@pytest.mark.parametrize("ticket,code", [(None, "step_expired"), ({"p": "wrong"}, "step_expired"),
    ({"e": 1}, "step_expired"), ({"a": "other"}, "sign_in_first")])
def test_done_rejects_invalid_or_foreign_tickets(hub, client, ticket, code):
    raw = "bad" if ticket is None else secretbox.encrypt_json(hub, {"p": "okta", "e": time.time() + 60,
        "n": "nonce", **ticket})
    r = client.get("/api/auth/saml/okta/done", params={"t": raw}, follow_redirects=False)
    assert r.headers["location"] == "/auth?error=" + code


def test_public_provider_lookup_fails_closed(hub, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("offline")
    monkeypatch.setattr(store, "list_providers", broken)
    assert routes.public_providers(hub) == []
