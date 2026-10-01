"""The Console's group routes (/portal/api/groups, contract 6.7) and group
grantees in the access editor API. Organization administrators only, decided
by the access store; same-origin mutations; errors as {"detail": {code, message}}."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import hubzoid.access as access
import hubzoid.db as db
from hubzoid import webapp_gateway
from hubzoid.auth import AuthUser
from hubzoid.portal import PortalAdmin, build_router

ADMIN = "boss@example.org"
DELEGATE = "lead@example.org"


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    monkeypatch.delenv("HUBZOID_UI", raising=False)
    monkeypatch.delenv("HUBZOID_AUTH", raising=False)
    monkeypatch.delenv("WEBUI_AUTH", raising=False)
    hub = tmp_path / "sales"
    (hub / "restricted").mkdir(parents=True)
    (hub / "restricted" / "crm_read.py").write_text("# restricted: read the CRM")
    (hub / "AGENTS.md").write_text("---\nname: Sales\n---\nYou help the sales team.\n")
    eng = create_engine(f"sqlite:///{tmp_path / 'op.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    access._stores.clear()
    gs = access.store_for(hub)
    gs.bootstrap([ADMIN], authoritative=True, hub="sales")
    gs.grant(DELEGATE, "sales", "manage_access", actor="test")
    gs.grant(DELEGATE, "sales", "crm_read", actor="test")
    yield hub
    access._stores.clear()


@pytest.fixture()
def client(hub, monkeypatch):
    who = {"email": ADMIN}

    def resolve(_request, _hub_dir):
        if who["email"] is None:
            return None
        return AuthUser(id="u-" + who["email"], email=who["email"], name="", role="admin",
                        method="password")

    monkeypatch.setattr("hubzoid.auth.sessions.resolve", resolve)

    def portal_admin(_request):
        if who["email"] is None:
            return None
        scope = access.service.AccessService(hub).scope(
            access.service.Actor(who["email"], "console", "session"))
        if not scope.any:
            return None
        return PortalAdmin(subject=who["email"], is_org_admin=scope.org_admin,
                           manageable=sorted(scope.hubs))

    app = FastAPI()
    webapp_gateway.mount(app, hub, model_label="sales")
    app.include_router(build_router(hub, admin_resolver=portal_admin))
    c = TestClient(app)
    c.headers.update({"origin": "http://testserver"})
    c.who = who  # type: ignore[attr-defined]
    c.gs = access.store_for(hub)  # type: ignore[attr-defined]
    return c


def _code(r):
    return r.json()["detail"]["code"]


def test_org_admin_manages_a_group_end_to_end(client):
    r = client.post("/portal/api/groups", json={"name": "  Sales   team ", "description": "Revenue",
                                                "emails": ["Ann@Example.org", "bob@example.org"]})
    assert r.status_code == 201, r.text
    group = r.json()["group"]
    gid = group["id"]
    assert group["name"] == "Sales team" and group["subject"] == f"group:{gid}"
    assert [m["email"] for m in group["members"]] == ["ann@example.org", "bob@example.org"]

    listed = client.get("/portal/api/groups").json()["groups"]
    assert [(g["id"], g["member_count"]) for g in listed] == [(gid, 2)]

    r = client.patch(f"/portal/api/groups/{gid}", json={"name": "Revenue"})
    assert r.status_code == 200 and r.json()["group"]["name"] == "Revenue"
    assert r.json()["group"]["description"] == "Revenue"          # untouched
    r = client.patch(f"/portal/api/groups/{gid}", json={"description": None})
    assert r.json()["group"]["description"] == ""

    r = client.post(f"/portal/api/groups/{gid}/members", json={"emails": ["carl@example.org", "ann@example.org"]})
    assert r.status_code == 200 and r.json()["added"] == ["carl@example.org"]
    assert client.delete(f"/portal/api/groups/{gid}/members/bob@example.org").status_code == 204
    assert _code(client.delete(f"/portal/api/groups/{gid}/members/bob@example.org")) == "not_member"
    detail = client.get(f"/portal/api/groups/{gid}").json()["group"]
    assert [m["email"] for m in detail["members"]] == ["ann@example.org", "carl@example.org"]

    assert client.delete(f"/portal/api/groups/{gid}").status_code == 204
    r = client.get(f"/portal/api/groups/{gid}")
    assert r.status_code == 404 and _code(r) == "not_found"


def test_only_organization_administrators(client):
    client.who["email"] = DELEGATE  # manages the hub, not the organization
    r = client.get("/portal/api/groups")
    assert r.status_code == 403 and _code(r) == "org_admin_required"
    r = client.post("/portal/api/groups", json={"name": "X"})
    assert r.status_code == 403
    client.who["email"] = "nobody@example.org"
    assert client.get("/portal/api/groups").status_code == 403


def test_signed_out_is_401(client):
    client.who["email"] = None
    r = client.get("/portal/api/groups")
    assert r.status_code == 401 and _code(r) == "unauthenticated"


def test_mutations_need_the_consoles_own_origin(client):
    gid = client.post("/portal/api/groups", json={"name": "Ops"}).json()["group"]["id"]
    for method, path, body in (
        ("post", "/portal/api/groups", {"name": "Other"}),
        ("patch", f"/portal/api/groups/{gid}", {"name": "Renamed"}),
        ("post", f"/portal/api/groups/{gid}/members", {"emails": ["a@example.org"]}),
        ("delete", f"/portal/api/groups/{gid}", None),
    ):
        kw = {"headers": {"origin": "https://evil.example"}}
        if body is not None:
            kw["json"] = body
        r = getattr(client, method)(path, **kw)
        assert r.status_code == 403 and _code(r) == "cross_origin", (method, path)
    assert client.get(f"/portal/api/groups/{gid}").json()["group"]["name"] == "Ops"


def test_validation_and_conflicts(client):
    assert _code(client.post("/portal/api/groups", json={"name": "a,b"})) == "invalid_name"
    assert _code(client.post("/portal/api/groups", json={"name": ""})) == "invalid_name"
    assert _code(client.post("/portal/api/groups", json={"name": "x" * 101})) == "invalid_name"
    assert _code(client.post("/portal/api/groups", json={"name": "Ok", "emails": ["nope"]})) == "invalid_email"
    assert _code(client.post("/portal/api/groups", json={"name": "Ok", "extra": 1})) == "invalid_request"
    client.post("/portal/api/groups", json={"name": "Finance"})
    r = client.post("/portal/api/groups", json={"name": "FINANCE"})
    assert r.status_code == 409 and _code(r) == "name_taken"
    assert client.get("/portal/api/groups/g_missing").status_code == 404
    assert client.patch("/portal/api/groups/g_missing", json={"name": "Z"}).status_code == 404


def test_group_detail_shows_each_members_account(client):
    gs = client.gs
    with gs.engine.begin() as conn:
        conn.execute(text("INSERT INTO hz_users (id, email, name, role, status, source, created_at, "
                          "updated_at) VALUES ('u1', 'ann@example.org', 'Ann Lee', 'user', 'active', "
                          "'admin', 0, 0), ('u2', 'pat@example.org', NULL, 'user', 'pending', 'signup', 0, 0)"))
    gid = client.post("/portal/api/groups", json={
        "name": "Ops", "emails": ["ann@example.org", "pat@example.org", "carl@example.org"]}).json()["group"]["id"]
    gs.suspend("carl@example.org", actor="test")   # blocking also leaves groups
    client.post(f"/portal/api/groups/{gid}/members", json={"emails": ["dee@example.org"]})
    members = {m["email"]: m for m in client.get(f"/portal/api/groups/{gid}").json()["group"]["members"]}
    assert set(members) == {"ann@example.org", "pat@example.org", "dee@example.org"}
    assert (members["ann@example.org"]["display"], members["ann@example.org"]["account"]) == ("Ann Lee", "active")
    assert members["pat@example.org"]["account"] == "pending"
    assert members["dee@example.org"]["account"] == "none" and members["dee@example.org"]["blocked"] is False


def test_blocked_people_are_not_added(client):
    client.gs.grant("tom@example.org", "sales", "use_hub", actor="test")
    client.gs.suspend("tom@example.org", actor="test")
    r = client.post("/portal/api/groups", json={"name": "Team", "emails": ["tom@example.org"]})
    assert r.status_code == 409 and _code(r) == "blocked"
    assert client.get("/portal/api/groups").json()["groups"] == []


def _access(client):
    return client.get("/portal/api/access", params={"hub": "sales"}).json()


def test_access_editor_gives_a_group_access_and_explains_it(client):
    gid = client.post("/portal/api/groups", json={
        "name": "Sales team", "emails": ["ann@example.org"]}).json()["group"]["id"]
    rev = _access(client)["revision"]
    r = client.post("/portal/api/access/apply", json={
        "subject": f"group:{gid}", "hub": "sales", "expected_revision": rev,
        "operations": [{"action": "grant", "permission": "crm_read"}]})
    assert r.status_code == 200, r.text

    acc = _access(client)
    rows = {row["subject"]: row for row in acc["rows"]}
    group_row = rows[f"group:{gid}"]
    assert group_row["kind"] == "group" and group_row["display"] == "Sales team"
    assert group_row["members"] == 1 and group_row["status"] == "group"
    assert set(group_row["perms"]) == {"use_hub", "crm_read"}
    ann = rows["ann@example.org"]  # listed although she holds nothing directly
    assert ann["perms"] == [] and set(ann["effective"]) == {"use_hub", "crm_read"}
    assert ann["via_groups"] == {"crm_read": ["Sales team"], "use_hub": ["Sales team"]}
    assert client.gs.can("ann@example.org", "sales", "crm_read")

    # People sees the same effective access.
    people = client.get("/portal/api/people").json()["people"]
    ann_person = next(p for p in people if p["subject"] == "ann@example.org")
    assert set(ann_person["access"]["sales"]) == {"use_hub", "crm_read"}

    # Deleting the group removes what it gave.
    client.delete(f"/portal/api/groups/{gid}")
    rows = {row["subject"]: row for row in _access(client)["rows"]}
    assert f"group:{gid}" not in rows and "ann@example.org" not in rows
    assert not client.gs.can("ann@example.org", "sales", "crm_read")


def test_access_editor_refuses_what_groups_cannot_hold(client):
    gid = client.post("/portal/api/groups", json={"name": "Leads"}).json()["group"]["id"]
    r = client.post("/portal/api/access/apply", json={
        "subject": f"group:{gid}", "hub": "sales",
        "operations": [{"action": "grant", "permission": "manage_access"}]})
    assert r.status_code == 422 and r.json()["code"] == "group_admin"
    r = client.post("/portal/api/access/apply", json={
        "subject": "group:g_missing", "hub": "sales",
        "operations": [{"action": "grant", "permission": "use_hub"}]})
    assert r.status_code == 404 and r.json()["code"] == "unknown_group"
    # A hub delegate can't give a group access (groups are organization-wide).
    client.who["email"] = DELEGATE
    r = client.post("/portal/api/access/apply", json={
        "subject": f"group:{gid}", "hub": "sales",
        "operations": [{"action": "grant", "permission": "crm_read"}]})
    assert r.status_code == 403
    assert not any(s == f"group:{gid}" for s, _h, _p in client.gs.list_grants())


def test_me_says_groups_exist_in_the_web_app_mode_only(client, monkeypatch):
    assert client.get("/portal/api/me").json()["groups"] is True
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    assert client.get("/portal/api/me").json()["groups"] is False


def test_agents_list_honours_group_access(client, hub):
    client.gs.set_authoritative(True, hub="sales")
    client.who["email"] = "ann@example.org"
    assert client.get("/api/agents").json()["agents"] == []
    client.who["email"] = ADMIN
    gid = client.post("/portal/api/groups", json={
        "name": "Sales team", "emails": ["ann@example.org"]}).json()["group"]["id"]
    client.post("/portal/api/access/apply", json={
        "subject": f"group:{gid}", "hub": "sales",
        "operations": [{"action": "grant", "permission": "use_hub"}]})
    client.who["email"] = "ann@example.org"
    agents = client.get("/api/agents").json()
    assert [a["id"] for a in agents["agents"]] == ["sales"] and agents["default_agent"] == "sales"
    assert agents["agents"][0]["api_base"] == ""
