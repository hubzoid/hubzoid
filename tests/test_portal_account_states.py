"""Account-state contract of the portal API: `/access` rows, `/people`, and
`/people/block` distinguish an admin block (`suspended`) from an Open WebUI
side unavailability (`account_unavailable`: pending approval or account gone).
See docs/PORTAL-ACCOUNT-CONTRACT.md."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import hubzoid.access as access
import hubzoid.db as db
from hubzoid.portal import PortalAdmin, build_router

FLAGS = ("suspended", "account_unavailable", "blocked", "status")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    (tmp_path / "restricted").mkdir()
    (tmp_path / "restricted" / "prod_in.py").write_text("# declared restricted permission")
    eng = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    access._stores.clear()
    gs = access.store_for(tmp_path)
    gs.set_authoritative(True)
    # a second org admin so blocking "root" style subjects never trips LastAdminError
    gs.grant("root", "*", "manage_access", actor="test")

    app = FastAPI()
    app.include_router(
        build_router(
            tmp_path,
            admin_resolver=lambda _r: PortalAdmin(
                subject="root", is_org_admin=True, manageable=[]
            ),
        )
    )
    c = TestClient(app)
    c.headers.update({"origin": "http://testserver"})
    c.gs = gs  # type: ignore[attr-defined]
    c.hub = tmp_path.name  # type: ignore[attr-defined]
    return c


def _access_row(client, subject):
    acc = client.get("/portal/api/access", params={"hub": client.hub}).json()
    rows = [r for r in acc["rows"] if r["subject"] == subject]
    return rows[0] if rows else None


def _person(client, subject):
    ppl = client.get("/portal/api/people", params={"q": subject}).json()["people"]
    rows = [p for p in ppl if p["subject"] == subject]
    return rows[0] if rows else None


def _state(row):
    return {k: row[k] for k in FLAGS}


def _grant(client, subject):
    r = client.post(
        "/portal/api/access/grant",
        json={"subject": subject, "hub": client.hub, "permission": "prod_in"},
    )
    return r


def test_pre_granted_email_is_awaiting_signup(client):
    assert _grant(client, "new@x.org").status_code == 200
    expected = dict(
        suspended=False, account_unavailable=False, blocked=False, status="awaiting-signup"
    )
    assert _state(_access_row(client, "new@x.org")) == expected
    assert _state(_person(client, "new@x.org")) == expected


def test_pending_owui_user_is_pending_approval_not_blocked(client):
    # An OWUI signup awaiting approval: owui_id bound, role=pending. The store
    # sets account_unavailable=1 for it; the old ladder rendered that as
    # "blocked". Pending must take precedence over the OWUI marker.
    assert _grant(client, "pend@x.org").status_code == 200
    client.gs.upsert_identity(email="pend@x.org", owui_id="u-pend", pending=True)
    expected = dict(
        suspended=False, account_unavailable=True, blocked=True, status="pending-approval"
    )
    assert _state(_access_row(client, "pend@x.org")) == expected
    assert _state(_person(client, "pend@x.org")) == expected
    # `blocked` stays the enforcement truth (nothing is weakened)
    assert client.gs.is_suspended("pend@x.org") is True


def test_deleted_owui_account_is_blocked_with_account_unavailable(client):
    assert _grant(client, "gone@x.org").status_code == 200
    client.gs.upsert_identity(email="gone@x.org", owui_id="u-gone", pending=False)
    assert _state(_access_row(client, "gone@x.org"))["status"] == "active"
    # a complete directory read that no longer lists the account
    client.gs.reconcile_accounts([])
    expected = dict(
        suspended=False, account_unavailable=True, blocked=True, status="blocked"
    )
    assert _state(_access_row(client, "gone@x.org")) == expected
    assert _state(_person(client, "gone@x.org")) == expected


def test_admin_block_reports_suspended(client):
    assert _grant(client, "bob@x.org").status_code == 200
    client.gs.upsert_identity(email="bob@x.org", owui_id="u-bob")
    r = client.post("/portal/api/people/block", json={"subject": "bob@x.org"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["changed"] is True and body["message"] is None
    assert _state(body) == dict(
        suspended=True, account_unavailable=False, blocked=True, status="blocked"
    )
    # suspend() cascades every grant, so the person leaves /access but stays in /people
    assert _access_row(client, "bob@x.org") is None
    assert _state(_person(client, "bob@x.org")) == _state(body)


def test_reactivate_clears_admin_block(client):
    _grant(client, "bob@x.org")
    client.gs.upsert_identity(email="bob@x.org", owui_id="u-bob")
    client.post("/portal/api/people/block", json={"subject": "bob@x.org"})
    r = client.post(
        "/portal/api/people/block", json={"subject": "bob@x.org", "suspended": False}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True and body["message"] is None
    assert _state(body) == dict(
        suspended=False, account_unavailable=False, blocked=False, status="active"
    )
    actions = [
        a["action"] for a in client.gs.read_access_audit(10, subject="bob@x.org")
    ]
    assert actions[0] == "reactivate"


def test_reactivate_when_only_owui_marker_is_set_is_honest_noop(client):
    _grant(client, "gone@x.org")
    client.gs.upsert_identity(email="gone@x.org", owui_id="u-gone")
    client.gs.reconcile_accounts([])
    r = client.post(
        "/portal/api/people/block", json={"subject": "gone@x.org", "suspended": False}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["changed"] is False
    assert _state(body) == dict(
        suspended=False, account_unavailable=True, blocked=True, status="blocked"
    )
    assert body["message"].startswith("Not blocked by an admin.")
    assert "Open WebUI" in body["message"]
    # the OWUI marker is untouched, enforcement unchanged, no bogus audit row
    assert client.gs.is_suspended("gone@x.org") is True
    actions = [
        a["action"] for a in client.gs.read_access_audit(10, subject="gone@x.org")
    ]
    assert "reactivate" not in actions
    assert actions[0] == "account_unavailable"


def test_reactivate_admin_block_on_pending_user_reports_remaining_block(client):
    _grant(client, "pend@x.org")
    client.gs.upsert_identity(email="pend@x.org", owui_id="u-pend", pending=True)
    client.post("/portal/api/people/block", json={"subject": "pend@x.org"})
    assert _state(_person(client, "pend@x.org"))["status"] == "blocked"  # admin wins
    r = client.post(
        "/portal/api/people/block", json={"subject": "pend@x.org", "suspended": False}
    )
    body = r.json()
    assert body["changed"] is True
    assert _state(body) == dict(
        suspended=False, account_unavailable=True, blocked=True, status="pending-approval"
    )
    assert body["message"].startswith("Admin block cleared.")


def test_grant_409_messages_name_the_real_cause(client):
    _grant(client, "bob@x.org")
    client.gs.upsert_identity(email="bob@x.org", owui_id="u-bob")
    client.post("/portal/api/people/block", json={"subject": "bob@x.org"})
    r = _grant(client, "bob@x.org")
    assert r.status_code == 409 and "Reactivate" in r.json()["detail"]

    _grant(client, "pend@x.org")
    client.gs.upsert_identity(email="pend@x.org", owui_id="u-pend", pending=True)
    r = _grant(client, "pend@x.org")
    assert r.status_code == 409
    assert "Reactivate" not in r.json()["detail"]
    assert "Open WebUI" in r.json()["detail"]


def test_block_endpoint_rejects_wildcard_and_last_admin(client):
    for body in ({"subject": "*"}, {"subject": "*", "suspended": False}):
        assert client.post("/portal/api/people/block", json=body).status_code == 409
    # "root" is the only org admin in this fixture
    r = client.post("/portal/api/people/block", json={"subject": "root"})
    assert r.status_code == 409 and "last org admin" in r.json()["detail"]


def test_revoke_still_allowed_for_blocked_rows(client):
    _grant(client, "pend@x.org")
    client.gs.upsert_identity(email="pend@x.org", owui_id="u-pend", pending=True)
    r = client.post(
        "/portal/api/access/revoke",
        json={"subject": "pend@x.org", "hub": client.hub, "permission": "prod_in"},
    )
    assert r.status_code == 200


def test_blocked_always_equals_store_is_suspended(client):
    _grant(client, "a@x.org")
    _grant(client, "b@x.org")
    _grant(client, "c@x.org")
    _grant(client, "workflow:nightly")
    client.gs.upsert_identity(email="a@x.org", owui_id="u-a", pending=True)
    client.gs.upsert_identity(email="b@x.org", owui_id="u-b")
    client.post("/portal/api/people/block", json={"subject": "b@x.org"})
    client.gs.upsert_identity(email="c@x.org", owui_id="u-c")
    client.gs.reconcile_accounts([{"id": "u-a", "email": "a@x.org", "role": "pending"}])
    people = client.get("/portal/api/people", params={"limit": 200}).json()["people"]
    assert {p["subject"] for p in people} >= {"a@x.org", "b@x.org", "c@x.org"}
    for p in people:
        assert p["blocked"] == client.gs.is_suspended(p["subject"])
        assert p["blocked"] == (p["suspended"] or p["account_unavailable"])
    acc = client.get("/portal/api/access", params={"hub": client.hub}).json()
    for row in acc["rows"]:
        assert row["blocked"] == client.gs.is_suspended(row["subject"])
    service = [r for r in acc["rows"] if r["subject"] == "workflow:nightly"][0]
    assert service["status"] == "service" and service["blocked"] is False
