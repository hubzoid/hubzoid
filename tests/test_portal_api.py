"""Tests for the admin portal JSON API (view-only except Access)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import hubzoid.access as access
import hubzoid.db as db
from hubzoid.portal import PortalAdmin, build_router


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
    # Writes are authorized by the access service from the store, not from the
    # injected resolver, so the test administrator is a real org admin there.
    gs.grant("root", "*", "manage_access", actor="test")

    admin = {"who": PortalAdmin(subject="root", is_org_admin=True, manageable=[])}

    def resolver(_request):
        return admin["who"]

    app = FastAPI()
    app.include_router(build_router(tmp_path, admin_resolver=resolver))
    c = TestClient(app)
    c.headers.update({"origin": "http://testserver"})   # same-origin for mutations
    c.gs = gs           # type: ignore[attr-defined]
    c.admin = admin     # type: ignore[attr-defined]
    c.hub = tmp_path.name  # type: ignore[attr-defined]
    return c


def test_me_and_forbidden(client):
    assert client.get("/portal/api/me").json()["org_admin"] is True
    client.admin["who"] = None
    assert client.get("/portal/api/me").status_code == 403


def test_permissions_and_hubs(client):
    r = client.get("/portal/api/permissions", params={"hub": client.hub})
    perms = {p["permission"] for p in r.json()["permissions"]}
    assert {"use_hub", "manage_access"} <= perms
    assert client.hub in {h["key"] for h in client.get("/portal/api/hubs").json()["hubs"]}


def test_grant_revoke_via_api(client):
    hub = client.hub
    r = client.post("/portal/api/access/grant",
                    json={"subject": "alice", "hub": hub, "permission": "prod_in"})
    assert r.status_code == 200
    acc = client.get("/portal/api/access", params={"hub": hub}).json()
    alice = [row for row in acc["rows"] if row["subject"] == "alice"][0]
    assert set(alice["perms"]) == {"prod_in", "use_hub"}   # implication surfaced
    assert acc["editable"] is True

    r = client.post("/portal/api/access/revoke",
                    json={"subject": "alice", "hub": hub, "permission": "prod_in"})
    assert r.status_code == 200
    acc = client.get("/portal/api/access", params={"hub": hub}).json()
    alice = [row for row in acc["rows"] if row["subject"] == "alice"][0]
    assert alice["perms"] == ["use_hub"]


def test_unavailable_account_can_be_offboarded(client):
    """An unavailable (chat-account-gone) person can have retained grants REMOVED and be
    explicitly blocked (offboarded), but must not receive NEW grants or be reactivated
    here (review finding). The hub is authoritative in this fixture."""
    hub = client.hub
    client.gs.grant("ghost@example.org", hub, "prod_in")  # retained grant (implies use_hub)
    with client.gs._engine.begin() as conn:
        conn.execute(
            text("INSERT INTO hz_meta(k, v) VALUES(:k, '1') ON CONFLICT (k) DO UPDATE SET v='1'"),
            {"k": "account_unavailable:ghost@example.org"},
        )
    # Granting NEW access to an unavailable account is refused...
    assert client.post("/portal/api/access/grant",
                       json={"subject": "ghost@example.org", "hub": hub, "permission": "prod_in"}).status_code == 409
    # ...but REMOVING a retained grant is allowed (offboarding).
    assert client.post("/portal/api/access/revoke",
                       json={"subject": "ghost@example.org", "hub": hub, "permission": "prod_in"}).status_code == 200
    # Explicit block offboards fully and drops every remaining grant.
    r = client.post("/portal/api/people/block", json={"subject": "ghost@example.org", "suspended": True})
    assert r.status_code == 200
    assert not [g for g in client.gs.list_grants(hub) if g[0] == "ghost@example.org"]


def test_legacy_hub_is_read_only_in_api(client):
    """A hub whose access is not yet dashboard-managed (Casbin not authoritative) is
    read-only: `/access` reports editable=false and every hub-scoped edit is refused,
    so a legacy agent's permissions can neither appear effective nor be silently
    overwritten by a later migration. Org-admin management stays available."""
    hub = client.hub
    client.gs.set_authoritative(False)  # make the hub legacy
    acc = client.get("/portal/api/access", params={"hub": hub}).json()
    assert acc["editable"] is False and acc["authoritative"] is False
    assert client.post("/portal/api/access/grant",
                       json={"subject": "alice", "hub": hub, "permission": "prod_in"}).status_code == 409
    assert client.post("/portal/api/access/revoke",
                       json={"subject": "alice", "hub": hub, "permission": "prod_in"}).status_code == 409
    assert client.post("/portal/api/access/apply", json={
        "subject": "alice", "hub": hub,
        "operations": [{"action": "grant", "permission": "prod_in"}],
    }).status_code == 409
    # Public toggle (a hub-scoped grant to '*') is refused too.
    assert client.post("/portal/api/access/grant",
                       json={"subject": "*", "hub": hub, "permission": "use_hub"}).status_code == 409
    # But org-admin management is not hub-scoped and remains available.
    assert client.post("/portal/api/access/grant",
                       json={"subject": "newadmin@example.org", "hub": "*", "permission": "manage_access"}).status_code == 200


def test_apply_atomic_change_set(client):
    hub = client.hub
    rev = client.get("/portal/api/access", params={"hub": hub}).json()["revision"]
    r = client.post("/portal/api/access/apply", json={
        "subject": "alice", "hub": hub, "expected_revision": rev,
        "operations": [{"action": "grant", "permission": "prod_in"}],
    })
    assert r.status_code == 200, r.text
    assert r.json()["revision"] > rev
    acc = client.get("/portal/api/access", params={"hub": hub}).json()
    alice = [row for row in acc["rows"] if row["subject"] == "alice"][0]
    assert set(alice["perms"]) == {"prod_in", "use_hub"}


def test_apply_unknown_permission_422_but_orphan_revoke_ok(client):
    hub = client.hub
    # A grant of a permission no longer in the catalogue (an "orphan").
    client.gs.grant("alice", hub, "legacy_tool")
    # Granting an unknown permission is refused.
    r = client.post("/portal/api/access/apply", json={
        "subject": "alice", "hub": hub,
        "operations": [{"action": "grant", "permission": "legacy_tool"}],
    })
    assert r.status_code == 422
    # But revoking the orphan is allowed.
    r = client.post("/portal/api/access/apply", json={
        "subject": "alice", "hub": hub,
        "operations": [{"action": "revoke", "permission": "legacy_tool"}],
    })
    assert r.status_code == 200, r.text
    acc = client.get("/portal/api/access", params={"hub": hub}).json()
    alice = [row for row in acc["rows"] if row["subject"] == "alice"]
    assert not alice or "legacy_tool" not in alice[0]["perms"]


def test_apply_hub_admin_cannot_grant_manage_access(client):
    hub = client.hub
    client.admin["who"] = PortalAdmin(subject="hubadmin", is_org_admin=False, manageable=[hub])
    r = client.post("/portal/api/access/apply", json={
        "subject": "alice", "hub": hub,
        "operations": [{"action": "grant", "permission": "manage_access"}],
    })
    assert r.status_code == 403


def test_apply_hub_admin_cannot_touch_other_hub(client):
    client.admin["who"] = PortalAdmin(subject="hubadmin", is_org_admin=False, manageable=[client.hub])
    r = client.post("/portal/api/access/apply", json={
        "subject": "alice", "hub": "some-other-hub",
        "operations": [{"action": "grant", "permission": "use_hub"}],
    })
    assert r.status_code == 403


def test_apply_to_unavailable_account_conflicts(client):
    hub = client.hub
    with client.gs._engine.begin() as conn:  # mark the chat account gone
        conn.execute(
            text("INSERT INTO hz_meta(k, v) VALUES(:k, '1') "
                 "ON CONFLICT (k) DO UPDATE SET v='1'"),
            {"k": "account_unavailable:ghost@example.org"},
        )
    r = client.post("/portal/api/access/apply", json={
        "subject": "ghost@example.org", "hub": hub,
        "operations": [{"action": "grant", "permission": "use_hub"}],
    })
    assert r.status_code == 409


def test_apply_rejects_org_and_wildcard(client):
    hub = client.hub
    assert client.post("/portal/api/access/apply", json={
        "subject": "alice", "hub": "*",
        "operations": [{"action": "grant", "permission": "manage_access"}],
    }).status_code == 400
    assert client.post("/portal/api/access/apply", json={
        "subject": "*", "hub": hub,
        "operations": [{"action": "grant", "permission": "use_hub"}],
    }).status_code == 403


def test_expected_revision_guards_concurrent_edits(client):
    """A save built on a stale policy revision is refused (optimistic concurrency)."""
    hub = client.hub
    acc = client.get("/portal/api/access", params={"hub": hub}).json()
    stale = acc["revision"]
    # Someone else changes access in the meantime, bumping the revision.
    assert client.post("/portal/api/access/grant",
                       json={"subject": "bob", "hub": hub, "permission": "use_hub"}).status_code == 200
    # Our edit, built on the old revision, is rejected rather than applied.
    r = client.post("/portal/api/access/grant",
                    json={"subject": "alice", "hub": hub, "permission": "use_hub",
                          "expected_revision": stale})
    assert r.status_code == 409
    assert "changed since you loaded it" in r.json()["detail"]
    # With the current revision it succeeds and returns the new revision.
    fresh = client.get("/portal/api/access", params={"hub": hub}).json()["revision"]
    ok = client.post("/portal/api/access/grant",
                     json={"subject": "alice", "hub": hub, "permission": "use_hub",
                           "expected_revision": fresh})
    assert ok.status_code == 200
    assert ok.json()["revision"] > stale


def test_hub_admin_cannot_grant_manage_access(client):
    hub = client.hub
    # demote to a hub admin (not org) who manages this hub and holds prod_in
    client.gs.grant("ha", hub, "manage_access", actor="test")
    client.gs.grant("ha", hub, "prod_in", actor="test")
    client.admin["who"] = PortalAdmin(subject="ha", is_org_admin=False, manageable=[hub])
    r = client.post("/portal/api/access/grant",
                    json={"subject": "x", "hub": hub, "permission": "manage_access"})
    assert r.status_code == 403
    # but can grant a normal permission in their hub, within what they hold
    r = client.post("/portal/api/access/grant",
                    json={"subject": "x", "hub": hub, "permission": "prod_in"})
    assert r.status_code == 200
    # the delegate ceiling: without prod_in themselves, they cannot grant it
    client.gs.revoke("ha", hub, "prod_in", actor="test")
    r = client.post("/portal/api/access/grant",
                    json={"subject": "y", "hub": hub, "permission": "prod_in"})
    assert r.status_code == 403 and r.json()["code"] == "outside_ceiling"


def test_hub_admin_cannot_touch_other_hub(client):
    client.admin["who"] = PortalAdmin(subject="ha", is_org_admin=False, manageable=["finance"])
    r = client.post("/portal/api/access/grant",
                    json={"subject": "x", "hub": "ops", "permission": "prod_in"})
    assert r.status_code == 403


def test_hub_admin_reads_scoped_to_managed_hubs(client):
    hub = client.hub
    # org admin seeds a second hub with a grant so it's "known"
    client.post("/portal/api/access/grant",
                json={"subject": "z", "hub": "otherhub", "permission": "prod_in"})
    client.admin["who"] = PortalAdmin(subject="ha", is_org_admin=False, manageable=[hub])
    # /hubs only lists managed hubs
    keys = {h["key"] for h in client.get("/portal/api/hubs").json()["hubs"]}
    assert hub in keys and "otherhub" not in keys
    # and reading another hub's access is forbidden
    assert client.get("/portal/api/access", params={"hub": "otherhub"}).status_code == 403


def test_reject_reserved_wildcard_grants(client):
    hub = client.hub
    # org admin cannot grant a tool permission to the wildcard subject
    r = client.post("/portal/api/access/grant",
                    json={"subject": "*", "hub": hub, "permission": "prod_in"})
    assert r.status_code == 403
    # nor create new access for everyone signed in
    r = client.post("/portal/api/access/grant",
                    json={"subject": "*", "hub": hub, "permission": "use_hub"})
    assert r.status_code == 403
    # the org domain only carries manage_access
    r = client.post("/portal/api/access/grant",
                    json={"subject": "y", "hub": "*", "permission": "prod_in"})
    assert r.status_code == 403


def test_cross_origin_mutation_refused(client):
    hub = client.hub
    r = client.post("/portal/api/access/grant",
                    headers={"origin": "http://evil.example"},
                    json={"subject": "a", "hub": hub, "permission": "prod_in"})
    assert r.status_code == 403
    # a bare POST with no Origin is also refused
    r = client.post("/portal/api/access/grant",
                    headers={"origin": ""},
                    json={"subject": "a", "hub": hub, "permission": "prod_in"})
    assert r.status_code == 403
    # Origin: null (opaque) is refused (no valid authority to compare)
    r = client.post("/portal/api/access/grant",
                    headers={"origin": "null"},
                    json={"subject": "a", "hub": hub, "permission": "prod_in"})
    assert r.status_code == 403


def test_overview_and_workflows_and_audit(client):
    client.post("/portal/api/access/grant",
                json={"subject": "alice", "hub": client.hub, "permission": "prod_in"})
    ov = client.get("/portal/api/overview").json()
    assert ov["authoritative"] is True and ov["grants"] >= 1
    assert "workflows" in client.get("/portal/api/workflows").json()
    assert "rows" in client.get("/portal/api/audit").json()


def test_people_filters_apply_before_pagination(client):
    hub = client.hub
    # 60 people so matches are guaranteed beyond the first page of 50.
    for i in range(60):
        client.gs.grant(f"user{i:02d}@example.org", hub, "use_hub")
    # Suspend three of them (spread so some fall past page 1 when unfiltered).
    for i in (5, 40, 55):
        client.gs.suspend(f"user{i:02d}@example.org", actor="root")
    r = client.get("/portal/api/people", params={"status": "blocked", "limit": 50}).json()
    subs = {p["subject"] for p in r["people"]}
    assert r["total"] == 3, r["total"]  # filtered total, not the page
    assert subs == {"user05@example.org", "user40@example.org", "user55@example.org"}
    # Role filter: only the org admin (root isn't an identity row; make one).
    client.gs.grant("boss@example.org", "*", "manage_access")
    admins = client.get("/portal/api/people", params={"role": "admin"}).json()["people"]
    assert all(p["organization_admin"] for p in admins)
    assert any(p["subject"] == "boss@example.org" for p in admins)


def test_people_agent_filter_respects_scope(client):
    # A hub admin cannot use the agent filter to read a hub they don't manage.
    client.admin["who"] = PortalAdmin(subject="hubadmin", is_org_admin=False, manageable=[client.hub])
    assert client.get("/portal/api/people", params={"agent": "other-hub"}).status_code == 403
    # In-scope agent filter is allowed.
    assert client.get("/portal/api/people", params={"agent": client.hub}).status_code == 200


def test_access_changes_filters_before_pagination(client):
    hub = client.hub
    client.gs.grant("alice@example.org", hub, "prod_in", actor="root")
    client.gs.revoke("alice@example.org", hub, "prod_in", actor="root")
    client.gs.grant("bob@example.org", hub, "use_hub", actor="other")
    only_revokes = client.get("/portal/api/access-changes", params={"action": "revoke"}).json()["rows"]
    assert only_revokes and all(r["action"] == "revoke" for r in only_revokes)
    by_actor = client.get("/portal/api/access-changes", params={"actor": "other"}).json()["rows"]
    assert by_actor and all(r["actor"] == "other" for r in by_actor)


def test_audit_filters_before_pagination(client, tmp_path):
    from hubzoid.access import audit as auditlib
    for i in range(60):
        auditlib.record(tmp_path, user=f"u{i}", surface="owui",
                        tool="ledger_read" if i % 2 else "payroll_run",
                        decision="deny" if i == 58 else "allow", reason="grant")
    denied = client.get("/portal/api/audit", params={"denied": True, "limit": 50}).json()["rows"]
    assert len(denied) == 1 and denied[0]["decision"] == "deny"  # found beyond page 1
    payroll = client.get("/portal/api/audit", params={"tool": "payroll_run", "limit": 200}).json()["rows"]
    assert payroll and all(r["tool"] == "payroll_run" for r in payroll)
