"""Company directories: SCIM provisioning and offboarding, SAML sign-in
against a real signing identity provider (pysaml2), and SCIM conformance."""
from __future__ import annotations

import base64
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hubzoid import auth
from hubzoid.auth import api_keys, routes, sessions, users
from hubzoid.auth.directory import saml, scim, store

from tests.fixtures.saml_idp import TestIdP
from tests.test_auth_mfa import hub  # noqa: F401 - fixture
from tests.test_auth_routes import ORIGIN, person, sign_in

BASE = "http://testserver"
CORE = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

pytestmark = pytest.mark.skipif(not saml.available()[0], reason="pysaml2 or xmlsec1 missing")


def app_for(hub) -> FastAPI:
    app = FastAPI()
    routes.mount(app, hub)
    scim.mount(app, hub)

    @app.get("/api/chat/ping")
    def ping(request: Request):
        return {"who": auth.require_user(request, hub).email}

    return app


@pytest.fixture
def directory(hub):
    sp_xml = saml.sp_metadata(BASE, {"id": "okta", "config": {}})
    idp = TestIdP("http://idp.test", sp_xml)
    xml = idp.metadata()
    summary = saml.idp_summary(xml)
    store.save_provider(hub, provider_id="okta", name="Okta", actor="test", enabled=True,
                        config={"metadata_xml": xml, "idp_entity_id": summary["entity_id"],
                                "idp_sso_url": summary["sso_url"]})
    _, token = store.create_token(hub, "okta", name="test", actor="test")
    c = TestClient(app_for(hub))
    return {"idp": idp, "client": c, "scim": {"authorization": f"Bearer {token}"}}


def provision(d, email="sam@example.com", active=True):
    r = d["client"].post("/scim/v2/okta/Users", headers=d["scim"], json={
        "schemas": [CORE], "userName": email, "externalId": "00u1", "active": active,
        "name": {"givenName": "Sam", "familyName": "Lee"},
        "emails": [{"value": email, "primary": True, "type": "work"}]})
    assert r.status_code == 201, r.text
    return r.json()


def saml_sign_in(d, email, *, sign=True, tamper=False, relay=True):
    c, idp = d["client"], d["idp"]
    start = c.get("/api/auth/saml/okta/login", follow_redirects=False)
    assert start.status_code == 302
    q = parse_qs(urlparse(start.headers["location"]).query)
    req = idp.parse_request(q["SAMLRequest"][0])
    _, body = idp.response(req, email, sign=sign)
    if tamper:
        xml = base64.b64decode(body).decode().replace(email, "mallory@example.com")
        body = base64.b64encode(xml.encode()).decode()
    form = {"SAMLResponse": body, "RelayState": q["RelayState"][0] if relay else ""}
    return c.post("/api/auth/saml/okta/acs", data=form, follow_redirects=False), form


def test_scim_creates_a_managed_account(hub, directory):
    created = provision(directory)
    user = users.find_by_email(hub, "sam@example.com")
    assert user["managed_by"] == "okta" and not user["has_password"] and user["name"] == "Sam Lee"
    found = directory["client"].get('/scim/v2/okta/Users?filter=userName eq "sam@example.com"',
                                    headers=directory["scim"]).json()
    assert found["totalResults"] == 1 and found["Resources"][0]["id"] == created["id"]
    # Taken email: never joined automatically.
    person(hub, email="ana@example.com")
    r = directory["client"].post("/scim/v2/okta/Users", headers=directory["scim"], json={
        "schemas": [CORE], "userName": "ana@example.com", "emails": [{"value": "ana@example.com"}]})
    assert r.status_code == 409 and r.json()["scimType"] == "uniqueness"


def test_scim_needs_its_own_providers_token(hub, directory):
    assert directory["client"].get("/scim/v2/okta/Users").status_code == 401
    store.save_provider(hub, provider_id="entra", name="Entra", actor="t", config={"metadata_xml": ""})
    assert directory["client"].get("/scim/v2/entra/Users", headers=directory["scim"]).status_code == 401
    assert directory["client"].post("/scim/v2/okta/Groups", headers=directory["scim"]).status_code == 501


def test_deactivation_ends_every_credential_in_one_step(hub, directory):
    created = provision(directory)
    resp, _ = saml_sign_in(directory, "sam@example.com")
    assert resp.status_code == 303, resp.headers.get("location")
    browser = directory["client"]
    assert browser.get("/api/chat/ping").json()["who"] == "sam@example.com"
    user = users.find_by_email(hub, "sam@example.com")
    _, key = api_keys.create(hub, sessions.local_owner(hub).__class__(
        id=user["id"], email=user["email"], role="user"), name="k", operations=["chat"])
    off = browser.patch(f"/scim/v2/okta/Users/{created['id']}", headers=directory["scim"], json={
        "schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": False}]})
    assert off.status_code == 200 and off.json()["active"] is False
    assert browser.get("/api/chat/ping").status_code == 401           # browser session
    assert TestClient(browser.app).get("/api/chat/ping",
                                       headers={"authorization": f"Bearer {key}"}).status_code == 401
    assert users.get(hub, user["id"])["status"] == "disabled"
    # A role change in Hubzoid doesn't lift the directory's hold.
    from hubzoid.access.accounts import HubzoidAccounts
    HubzoidAccounts(hub).update(user["id"], role="admin")
    assert users.get(hub, user["id"])["status"] == "disabled"
    # A change made from before the deactivation is refused, not applied over it.
    stale = browser.put(f"/scim/v2/okta/Users/{created['id']}",
                        headers={**directory["scim"], "If-Match": created["meta"]["version"]},
                        json={**created, "displayName": "Sam L", "active": True})
    assert stale.status_code == 412
    # The same race inside one write (read before, written after): refused too.
    with pytest.raises(store.DirectoryError) as raced:
        store.update(hub, "okta", created["id"], user_name="sam@example.com", name="Sam L",
                     external_id="00u1", active=True, resource={}, expected_version=1)
    assert raced.value.status == 412 and users.get(hub, user["id"])["status"] == "disabled"
    browser.cookies.clear()
    again, _ = saml_sign_in(directory, "sam@example.com")
    assert "account_disabled" in again.headers["location"]
    # Only the directory brings the account back.
    on = browser.patch(f"/scim/v2/okta/Users/{created['id']}", headers=directory["scim"], json={
        "schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": True}]})
    assert on.json()["active"] is True and users.get(hub, user["id"])["status"] == "active"
    # DELETE turns it off for good but keeps the account (and its conversations).
    assert browser.delete(f"/scim/v2/okta/Users/{created['id']}",
                          headers=directory["scim"]).status_code == 204
    assert users.get(hub, user["id"])["status"] == "disabled"


def test_saml_refuses_replay_tampering_unsigned_and_strangers(hub, directory):
    provision(directory)
    ok, form = saml_sign_in(directory, "sam@example.com")
    assert ok.status_code == 303 and "error" not in ok.headers["location"]
    directory["client"].cookies.clear()
    replayed = directory["client"].post("/api/auth/saml/okta/acs", data=form, follow_redirects=False)
    assert "saml_replayed" in replayed.headers["location"]
    for kwargs, code in (({"tamper": True}, "saml_invalid_response"),
                         ({"sign": False}, "saml_invalid_response")):
        directory["client"].cookies.clear()
        r, _ = saml_sign_in(directory, "sam@example.com", **kwargs)
        assert code in r.headers["location"], (kwargs, r.headers["location"])
    directory["client"].cookies.clear()
    stranger, _ = saml_sign_in(directory, "nobody@example.com")
    assert "not_provisioned" in stranger.headers["location"]


def test_unsolicited_answers_start_a_fresh_sign_in(hub, directory):
    provision(directory)
    r, _ = saml_sign_in(directory, "sam@example.com", relay=False)
    assert r.status_code == 303 and r.headers["location"] == "/api/auth/saml/okta/login"
    again, _ = saml_sign_in(directory, "sam@example.com", relay=False)
    assert "saml_unsolicited" in again.headers["location"]  # bounced once a minute at most


def test_managed_accounts_have_no_password(hub, directory):
    provision(directory)
    user = users.find_by_email(hub, "sam@example.com")
    with pytest.raises(users.InvalidAccount):
        users.set_password(hub, user["id"], "a password of mine")
    # People says so (not "signs in with Google").
    from hubzoid.access.accounts import HubzoidAccounts
    assert HubzoidAccounts(hub).get(user["id"])["sign_in"] == "directory"


def test_admin_api_shows_values_to_paste_and_makes_tokens(hub, directory):
    person(hub, email="boss@example.com", role="admin")
    admin = TestClient(directory["client"].app)
    sign_in(admin, email="boss@example.com")
    listed = admin.get("/api/auth/directories").json()
    okta = listed["providers"][0]
    assert okta["paste"]["acs_url"] == "http://testserver/api/auth/saml/okta/acs"
    made = admin.post("/api/auth/directories/okta/scim-tokens", json={"name": "Okta"}, headers=ORIGIN)
    assert made.status_code == 201 and made.json()["token"].startswith("hzs_")
    session = admin.get("/api/auth/session").json()
    assert {"id": "okta", "name": "Okta", "kind": "saml",
            "login_url": "/api/auth/saml/okta/login"} in session["providers"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_scim_conformance(hub, directory):
    """scim2-tester's checks for Users (Groups are not offered this release)."""
    import httpx2
    import uvicorn
    from scim2_client.engines.httpx2 import SyncSCIMClient
    from scim2_tester import Status, check_server

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(directory["client"].app, host="127.0.0.1", port=port,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        client = SyncSCIMClient(httpx2.Client(base_url=f"http://127.0.0.1:{port}/scim/v2/okta",
                                              headers=directory["scim"]))
        client.discover()
        results = check_server(client, resource_types=["User"])
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    failed = [f"{r.title}: {r.reason}" for r in results if r.status == Status.ERROR]
    assert not failed, "\n".join(failed)
