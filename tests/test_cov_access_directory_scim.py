"""SCIM discovery, projection, validation, conditional writes and token boundaries."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hubzoid.auth import users
from hubzoid.auth.directory import saml, scim, store
from tests.test_auth_mfa import hub  # noqa: F401

pytestmark = pytest.mark.skipif(not saml.available()[0], reason="pysaml2 or xmlsec1 missing")
ROOT = "/scim/v2/okta"


@pytest.fixture
def client(hub):
    store.save_provider(hub, provider_id="okta", name="Okta", config={}, actor="test")
    _, token = store.create_token(hub, "okta", name="test", actor="test")
    app = FastAPI()
    scim.mount(app, hub)
    return TestClient(app, headers={"authorization": f"Bearer {token}"})


def create(client, user_name="ana@example.com", **kw):
    r = client.post(ROOT + "/Users", json={"userName": user_name, **kw})
    assert r.status_code == 201, r.text
    assert r.headers["etag"] == r.json()["meta"]["version"]
    return r.json()


@pytest.mark.parametrize("path", ["ServiceProviderConfig", "ResourceTypes", "Schemas",
    "Schemas/unknown", "ResourceTypes/User", "ResourceTypes/Group", "Users", "Users/missing",
    "Groups", "unknown"])
def test_endpoints_require_provider_token(client, path):
    r = client.get(ROOT + "/" + path, headers={"authorization": "Basic wrong"})
    assert r.status_code == 401 and r.json()["status"] == "401"
    assert r.headers["content-type"].startswith(scim.MEDIA)


def test_discovery(client):
    cfg = client.get(ROOT + "/ServiceProviderConfig")
    assert cfg.status_code == 200 and cfg.json()["patch"]["supported"]
    assert not cfg.json()["bulk"]["supported"]
    types = client.get(ROOT + "/ResourceTypes").json()
    assert types["totalResults"] == 1 and types["Resources"][0]["endpoint"] == "/Users"
    assert client.get(ROOT + "/ResourceTypes/User").json()["schema"] == scim.CORE_USER
    schemas = client.get(ROOT + "/Schemas").json()
    assert schemas["Resources"][0]["id"] == scim.CORE_USER
    assert client.get(ROOT + "/Schemas/" + scim.CORE_USER).json()["id"] == scim.CORE_USER
    for path in ("Schemas/unknown", "ResourceTypes/Group", "unknown"):
        assert client.get(ROOT + "/" + path).status_code == 404
    assert client.post(ROOT + "/Schemas", json={}).status_code == 405
    assert client.post(ROOT + "/Groups", json={}).status_code == 501


def test_create_scrubs_password_and_supports_internal_addresses(hub, client):
    a = create(client, "Employee 12", displayName="Employee", password="do-not-store", id="client-id", meta={})
    assert a["id"] != "client-id" and "password" not in a
    user = users.find_by_email(hub, "employee-12@okta.directory.invalid")
    assert user["name"] == "Employee" and user["managed_by"] == "okta"
    b = create(client, "ben", name={"givenName": "Ben", "familyName": "Lee"},
               emails=[{"value": "old@example.com"}, {"value": "BEN@EXAMPLE.COM", "primary": True}])
    assert users.find_by_email(hub, "ben@example.com")["name"] == "Ben Lee"
    assert b["userName"] == "ben"
    c = create(client, "plain@example.com", active=False)
    assert c["active"] is False
    assert users.find_by_email(hub, "plain@example.com")["status"] == "disabled"


@pytest.mark.parametrize("body", [{}, {"userName": 123}, {"userName": ""}, {"emails": "bad"}])
def test_invalid_users_are_refused(client, body):
    r = client.post(ROOT + "/Users", json=body)
    assert r.status_code == 400 and r.json()["scimType"] == "invalidValue"


def test_search_paging_projection_and_filters(client):
    a = create(client, displayName="Ana", externalId="ext-1")
    create(client, "ben@example.com", displayName="Ben")
    r = client.get(ROOT + "/Users", params={"startIndex": 0, "count": 1,
        "attributes": scim.CORE_USER + ":userName,name.givenName", "excludedAttributes": "name"})
    assert r.status_code == 200
    page = r.json()
    assert (page["totalResults"], page["startIndex"], page["itemsPerPage"]) == (2, 1, 1)
    assert set(page["Resources"][0]) == {"schemas", "id", "meta", "userName"}
    found = client.post(ROOT + "/Users/.search", json={"filter": 'userName eq "ana@example.com"',
        "attributes": ["externalId"], "excludedAttributes": ["meta"]}).json()
    assert found["Resources"][0]["externalId"] == "ext-1"
    assert found["Resources"][0]["id"] == a["id"] and "meta" in found["Resources"][0]
    assert client.get(ROOT + "/Users", params={"count": -1}).json()["Resources"] == []
    assert client.get(ROOT + "/Users", params={"filter": "!!!"}).json()["scimType"] == "invalidFilter"
    assert client.post(ROOT + "/.search", content="bad").json()["scimType"] == "invalidSyntax"
    view = client.get(ROOT + "/Users/" + a["id"], params={"excludedAttributes": "displayName"}).json()
    assert "displayName" not in view and view["id"] == a["id"]


def test_replace_patch_and_stale_writes(client):
    a = create(client)
    url = ROOT + "/Users/" + a["id"]
    r = client.put(url, headers={"If-Match": a["meta"]["version"]},
                   json={"userName": "renamed@example.com", "displayName": "Renamed"})
    assert r.status_code == 200 and r.json()["userName"] == "renamed@example.com"
    assert r.headers["etag"] == 'W/"2"'
    stale = client.patch(url, headers={"If-Match": 'W/"1"'}, json={})
    assert stale.status_code == 412
    assert client.put(url, headers={"If-Match": 'W/"1"'}, json={}).status_code == 412
    assert client.put(url, json={}).status_code == 400
    assert client.put(url, json={"userName": {"bad": "value"}}).status_code == 400
    assert client.patch(url, json={"Operations": [{"op": "bad"}]}).json()["scimType"] == "invalidValue"
    patched = client.patch(url, json={"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "replace", "path": "active", "value": False}]})
    assert patched.status_code == 200 and patched.json()["active"] is False
    assert client.delete(url).status_code == 204
    for method in (client.get, client.put, client.patch, client.delete):
        assert method(url).status_code == 404


def test_duplicate_create_and_rename_refuse_takeover(client):
    a = create(client)
    b = create(client, "ben@example.com")
    assert client.post(ROOT + "/Users", json={"userName": a["userName"]}).status_code == 409
    conflict = client.put(ROOT + "/Users/" + b["id"], json={"userName": a["userName"]})
    assert conflict.status_code == 409 and conflict.json()["scimType"] == "uniqueness"


@pytest.mark.parametrize("method,path", [("post", "Users"), ("post", ".search"),
    ("put", "Users/missing"), ("patch", "Users/missing"), ("delete", "Users/missing"),
    ("post", "Groups"), ("post", "unknown")])
def test_writes_require_token(client, method, path):
    r = getattr(client, method)(ROOT + "/" + path, headers={"authorization": "Bearer invalid"})
    assert r.status_code == 401 and r.json()["detail"].startswith("A valid SCIM token")
