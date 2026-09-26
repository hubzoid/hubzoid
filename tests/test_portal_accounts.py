"""The management API over HTTP: accounts, change requests, `/me`, and callers
holding an Open WebUI API key (`Authorization: Bearer sk-...`).

Identity comes from the production resolver (its local-development override,
which skips only the Open WebUI cookie check) or from a stubbed API-key lookup.
The chat app is the fake Open WebUI from test_access_service.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hubzoid.access import accounts as accountlib
from hubzoid.access import owui_api_keys
from hubzoid.access.service import Actor
from hubzoid.portal import build_router

from tests.test_access_service import (  # noqa: F401 — shared fixture and helpers
    DELEGATE,
    ROOT,
    dep,
)

PASSWORD = "Correct-Horse-7-battery"
KEYS = {"sk-root": ROOT, "sk-dele": DELEGATE, "sk-ann": "ann@x.org"}


@pytest.fixture
def api(dep, monkeypatch):
    monkeypatch.setattr(accountlib, "for_deployment", lambda *_a, **_k: dep.directory)
    monkeypatch.setattr(owui_api_keys, "resolve_email", lambda _h, token: KEYS.get(token))
    monkeypatch.setenv("HUBZOID_PORTAL_DEV", "1")
    app = FastAPI()
    app.include_router(build_router(dep.hub_dir))
    client = TestClient(app)

    def as_(subject):
        monkeypatch.setenv("HUBZOID_PORTAL_DEV_USER", subject)
        client.headers.update({"origin": "http://testserver"})
        client.headers.pop("authorization", None)
        return client

    def key(token):
        monkeypatch.setenv("HUBZOID_PORTAL_DEV_USER", "")
        client.headers.pop("origin", None)
        client.headers["authorization"] = f"Bearer {token}"
        return client

    dep.as_, dep.key, dep.client = as_, key, client
    return dep


def _create(client, grants, email="ann@x.org"):
    return client.post("/portal/api/accounts", json=dict(
        email=email, name="Ann", password=PASSWORD,
        grants=[dict(hub=h, permission=p) for h, p in grants]))


def test_me_reports_grantable_and_account_rights(api):
    me = api.as_(DELEGATE).get("/portal/api/me").json()
    assert me["grantable"] == {"finance": ["ledger", "use_hub"]}
    assert me["account_admin"] is False and me["can_create_accounts"] is True
    assert me["accounts_configured"] is True and me["via"] == "session"
    root = api.as_(ROOT).get("/portal/api/me").json()
    assert root["account_admin"] is True and "payroll" in root["grantable"]["finance"]
    brief = api.as_(DELEGATE).get("/portal/api/me", params={"brief": 1}).json()
    assert "grantable" not in brief and brief["subject"] == DELEGATE
    assert api.as_("ann@x.org").get("/portal/api/me").status_code == 403


def test_access_view_carries_the_viewers_ceiling(api):
    body = api.as_(DELEGATE).get("/portal/api/access", params={"hub": "finance"}).json()
    assert body["grantable"] == ["ledger", "use_hub"] and body["viewer"] == DELEGATE


def test_delegate_creates_account_within_ceiling(api):
    r = _create(api.as_(DELEGATE), [("finance", "ledger")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subject"] == "ann@x.org" and PASSWORD not in r.text
    assert api.gs.can("ann@x.org", "finance", "ledger")
    assert api.owui.by_email("ann@x.org")["role"] == "user"
    audit = api.gs.read_access_audit(10, subject="ann@x.org")
    assert {r["surface"] for r in audit} == {"console"}


def test_delegate_wider_grant_is_refused_server_side(api):
    r = _create(api.as_(DELEGATE), [("finance", "payroll")])
    assert r.status_code == 403 and r.json()["code"] == "outside_ceiling"
    r = _create(api.as_(DELEGATE), [("ops", "use_hub")])
    assert r.status_code == 403
    assert api.owui.by_email("ann@x.org") is None


def test_existing_account_returns_a_code_the_ui_can_act_on(api):
    api.owui.add_user("ann@x.org", "Ann")
    r = _create(api.as_(ROOT), [("finance", "use_hub")])
    assert r.status_code == 409 and r.json()["code"] == "account_exists"


def test_cookie_mutations_need_same_origin(api):
    client = api.as_(ROOT)
    r = client.post("/portal/api/accounts", headers={"origin": "http://evil.example"}, json=dict(
        email="ann@x.org", name="Ann", password=PASSWORD, grants=[]))
    assert r.status_code == 403
    assert api.owui.by_email("ann@x.org") is None


def test_validation_errors_never_echo_the_password(api):
    client = api.as_(ROOT)
    for body in (
        dict(email="ann@x.org", name="Ann", password=[PASSWORD], grants=[]),
        dict(email="ann@x.org", name="", password=PASSWORD, grants=[]),
        dict(email="ann@x.org", name="Ann", password=PASSWORD, grants=[], extra=1),
        [PASSWORD],
    ):
        r = client.post("/portal/api/accounts", json=body)
        assert r.status_code == 422 and PASSWORD not in r.text, r.text
    r = client.post("/portal/api/accounts/ann@x.org/password", json={"password": PASSWORD, "x": 1})
    assert r.status_code == 422 and PASSWORD not in r.text


def test_api_key_callers(api):
    # A key holder acts as its owner, without an Origin header.
    r = _create(api.key("sk-dele"), [("finance", "ledger")])
    assert r.status_code == 200, r.text
    assert api.key("sk-dele").get("/portal/api/me").json()["via"] == "api-key"
    rows = api.gs.read_access_audit(10, subject="ann@x.org")
    assert rows and all(r["surface"] == "api" and r["actor"] == DELEGATE for r in rows)
    # The ceiling holds for key callers too.
    r = api.key("sk-dele").post("/portal/api/access/grant", json=dict(
        subject="ann@x.org", hub="finance", permission="payroll"))
    assert r.status_code == 403
    assert api.key("sk-unknown").get("/portal/api/me").status_code == 401
    # Any other Authorization value (HTTP Basic from a proxy, a chat-app token)
    # is ignored: the request is a cookie session as before, here with no user.
    for value in ("Bearer not-an-sk-key", "Basic cm9vdDpwdw=="):
        api.client.headers["authorization"] = value
        assert api.client.get("/portal/api/me").status_code == 401
    api.as_(DELEGATE).headers["authorization"] = "Basic cm9vdDpwdw=="
    assert api.client.get("/portal/api/me").json()["via"] == "session"
    # A valid key for someone who manages nothing opens nothing.
    assert api.key("sk-ann").get("/portal/api/me").status_code == 403


def test_org_admin_account_actions(api):
    uid = api.owui.add_user("bob@x.org", "Bob", role="pending")
    api.gs.upsert_identity(email="bob@x.org", owui_id=uid, pending=True)
    assert api.as_(DELEGATE).post("/portal/api/accounts/bob@x.org/approve").status_code == 403
    client = api.as_(ROOT)
    assert client.get("/portal/api/accounts/bob@x.org").json()["role"] == "pending"
    assert api.as_(DELEGATE).get("/portal/api/accounts/bob@x.org").status_code == 403
    client = api.as_(ROOT)
    assert client.post("/portal/api/accounts/bob@x.org/approve").status_code == 200
    assert client.get("/portal/api/accounts/bob@x.org").json()["role"] == "user"
    r = client.post("/portal/api/accounts/bob@x.org/password", json={"password": PASSWORD})
    assert r.status_code == 200 and PASSWORD not in r.text
    assert client.post("/portal/api/accounts/bob@x.org/role", json={"role": "admin"}).status_code == 200
    assert api.owui.users[uid]["role"] == "admin"
    assert client.post("/portal/api/accounts/bob@x.org/role", json={"role": "owner"}).status_code == 422
    r = client.request("DELETE", "/portal/api/accounts/bob@x.org", json={"confirm_email": "b@x.org"})
    assert r.status_code == 422 and uid in api.owui.users
    r = client.request("DELETE", "/portal/api/accounts/bob@x.org", json={"confirm_email": "BOB@x.org"})
    assert r.status_code == 200 and uid not in api.owui.users


def test_change_request_endpoints(api):
    proposed = api.svc.propose(Actor(DELEGATE, "whatsapp", "bridge"), dict(
        kind="access", hub="finance", subject="ann@x.org", grant=["ledger"]))
    rid = proposed["id"]
    # Only the proposer sees it.
    assert api.as_(ROOT).get(f"/portal/api/change-requests/{rid}").status_code == 404
    view = api.as_(DELEGATE).get(f"/portal/api/change-requests/{rid}").json()
    assert view["status"] == "pending" and view["surface"] == "whatsapp"
    # An API key cannot confirm; a cross-site page cannot either.
    r = api.key("sk-dele").post(f"/portal/api/change-requests/{rid}/confirm",
                                json={"plan_hash": view["plan_hash"]})
    assert r.status_code == 403 and r.json()["code"] == "session_required"
    r = api.as_(DELEGATE).post(f"/portal/api/change-requests/{rid}/confirm",
                               headers={"origin": "http://evil.example"},
                               json={"plan_hash": view["plan_hash"]})
    assert r.status_code == 403
    r = api.as_(DELEGATE).post(f"/portal/api/change-requests/{rid}/confirm",
                               json={"plan_hash": view["plan_hash"]})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    assert api.gs.can("ann@x.org", "finance", "ledger")
    r = api.as_(DELEGATE).post(f"/portal/api/change-requests/{rid}/confirm",
                               json={"plan_hash": view["plan_hash"]})
    assert r.status_code == 409


def test_change_request_reject_endpoint(api):
    rid = api.svc.propose(Actor(ROOT, "owui", "bridge"), dict(
        kind="access", hub="ops", subject="ann@x.org", grant=["inventory"]))["id"]
    r = api.as_(ROOT).post(f"/portal/api/change-requests/{rid}/reject")
    assert r.status_code == 200
    assert api.as_(ROOT).get(f"/portal/api/change-requests/{rid}").json()["status"] == "rejected"


def test_account_proposal_confirmed_with_password(api):
    rid = api.svc.propose(Actor(DELEGATE, "mcp", "bridge"), dict(
        kind="account", hub="finance", email="new@x.org", name="New"))["id"]
    client = api.as_(DELEGATE)
    view = client.get(f"/portal/api/change-requests/{rid}").json()
    r = client.post(f"/portal/api/change-requests/{rid}/confirm",
                    json={"plan_hash": view["plan_hash"], "password": PASSWORD})
    assert r.status_code == 200 and PASSWORD not in r.text
    assert api.owui.by_email("new@x.org")


# ---- Add user: existing accounts, Google sign-in only, partial results ----------------

def _manifest_sign_in(api, **flags):
    import json

    pointer = json.loads((api.hub_dir / ".hubzoid" / "deployment.json").read_text())["manifest"]
    data = json.loads(open(pointer).read())
    data["sign_in"] = flags
    open(pointer, "w").write(json.dumps(data))


def test_me_reports_sign_in_modes(api):
    assert api.as_(ROOT).get("/portal/api/me").json()["sign_in"] == {"password": True, "google": False}
    _manifest_sign_in(api, google=True, merge_by_email=True, allowed_domains=["x.org"])
    assert api.as_(DELEGATE).get("/portal/api/me").json()["sign_in"] == {
        "password": True, "google": True, "google_domains": ["x.org"]}


def test_account_search_is_scoped_like_people(api):
    for email, name in (("ann@x.org", "Ann Lee"), ("bob@x.org", "Bob Lee"), ("cy@x.org", "Cy")):
        api.gs.upsert_identity(email=email, owui_id=api.owui.add_user(email, name), display=name)
    api.gs.grant("bob@x.org", "finance", "use_hub", actor="test")
    api.gs.grant("pre@x.org", "finance", "use_hub", actor="test")  # email only: not an account
    api.gs.grant("workflow:close", "finance", "use_hub", actor="test")
    found = lambda client, q: [r["subject"] for r in client.get(  # noqa: E731
        "/portal/api/accounts", params={"q": q}).json()["accounts"]]
    assert found(api.as_(ROOT), "lee") == ["ann@x.org", "bob@x.org"]
    assert "pre@x.org" not in found(api.as_(ROOT), "") and "workflow:close" not in found(api.as_(ROOT), "")
    # A delegate sees accounts in their agents, and one account by its full email.
    assert found(api.as_(DELEGATE), "lee") == ["bob@x.org"]
    assert found(api.as_(DELEGATE), "cy") == []
    assert found(api.as_(DELEGATE), "CY@x.org") == ["cy@x.org"]
    row = api.as_(ROOT).get("/portal/api/accounts", params={"q": "ann"}).json()["accounts"][0]
    assert row["display"] == "Ann Lee" and row["status"] == "active" and row["organization_admin"] is False
    assert api.as_("nobody@x.org").get("/portal/api/accounts").status_code == 403


def test_grant_to_existing_account_endpoint(api):
    api.gs.upsert_identity(email="ann@x.org", owui_id=api.owui.add_user("ann@x.org", "Ann"))
    client = api.as_(DELEGATE)
    r = client.post("/portal/api/accounts/grant",
                    json=dict(email="ann@x.org", grants=[dict(hub="finance", permission="ledger")]))
    assert r.status_code == 200 and r.json()["grants"] == {"finance": ["ledger"]}
    r = client.post("/portal/api/accounts/grant",
                    json=dict(email="ann@x.org", grants=[dict(hub="finance", permission="payroll")]))
    assert r.status_code == 403 and r.json()["code"] == "outside_ceiling"
    r = client.post("/portal/api/accounts/grant",
                    json=dict(email="nobody@x.org", grants=[dict(hub="finance", permission="use_hub")]))
    assert r.status_code == 404 and r.json()["code"] == "no_account"
    assert not api.gs.can("nobody@x.org", "finance", "use_hub")
    r = client.post("/portal/api/accounts/grant", headers={"origin": "http://evil.example"},
                    json=dict(email="ann@x.org", grants=[dict(hub="finance", permission="use_hub")]))
    assert r.status_code == 403


def test_duplicate_then_grant_instead(api):
    api.owui.add_user("ann@x.org", "Ann")  # in the chat app, not yet recorded here
    r = _create(api.as_(DELEGATE), [("finance", "ledger")])
    assert r.status_code == 409 and r.json()["code"] == "account_exists"
    r = api.as_(DELEGATE).post("/portal/api/accounts/grant", json=dict(
        email="ann@x.org", grants=[dict(hub="finance", permission="ledger")]))
    assert r.status_code == 200, r.text
    assert api.gs.can("ann@x.org", "finance", "ledger")
    assert api.gs.identity("ann@x.org")["owui_id"] == api.owui.by_email("ann@x.org")["id"]


def test_partial_create_reports_the_account_and_retry_does_not_duplicate(api, monkeypatch):
    original = api.gs.bind_new_account

    def grants_fail(*a, **k):
        if k.get("grants"):
            raise RuntimeError("database went away")
        return original(*a, **k)

    monkeypatch.setattr(api.gs, "bind_new_account", grants_fail)
    r = _create(api.as_(ROOT), [("finance", "ledger")])
    monkeypatch.setattr(api.gs, "bind_new_account", original)
    body = r.json()
    assert r.status_code == 502 and body["code"] == "partial" and PASSWORD not in r.text
    assert body["account"] == {"subject": "ann@x.org", "name": "Ann", "sign_in": "password"}
    assert body["access_granted"] is False
    assert _create(api.as_(ROOT), [("finance", "ledger")]).json()["code"] == "account_exists"
    r = api.as_(ROOT).post("/portal/api/accounts/grant", json=dict(
        email="ann@x.org", grants=[dict(hub="finance", permission="ledger")]))
    assert r.status_code == 200 and api.gs.can("ann@x.org", "finance", "ledger")
    assert sum(u["email"] == "ann@x.org" for u in api.owui.users.values()) == 1


def test_google_sign_in_only_over_http(api, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    body = dict(email="ann@x.org", name="Ann", sign_in="google",
                grants=[dict(hub="finance", permission="ledger")])
    r = api.as_(DELEGATE).post("/portal/api/accounts", json=body)
    assert r.status_code == 409 and r.json()["code"] == "google_unavailable"
    assert api.owui.by_email("ann@x.org") is None
    _manifest_sign_in(api, google=True, merge_by_email=True)
    r = api.as_(DELEGATE).post("/portal/api/accounts", json={**body, "password": PASSWORD})
    assert r.status_code == 422 and PASSWORD not in r.text
    r = api.as_(DELEGATE).post("/portal/api/accounts", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["sign_in"] == "google" and "password" not in r.json()
    sent = next(b["password"] for m, p, b in api.owui.requests
                if p == "/api/v1/auths/add" and b["email"] == "ann@x.org")
    assert sent not in r.text and sent not in caplog.text
    audit = json.dumps(api.gs.read_access_audit(50), default=str)
    assert sent not in audit and "account_create" in audit
    # A password-mode create still needs a password.
    r = api.as_(ROOT).post("/portal/api/accounts", json=dict(
        email="bob@x.org", name="Bob", grants=[]))
    assert r.status_code == 422 and r.json()["code"] == "invalid_password"
