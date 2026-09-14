"""Tests for the admin portal JSON API (view-only except Access)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import hubzoid.access as access
import hubzoid.db as db
from hubzoid.portal import PortalAdmin, build_router


@pytest.fixture()
def client(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    access._stores.clear()
    gs = access.store_for(tmp_path)
    gs.set_authoritative(True)

    admin = {"who": PortalAdmin(subject="root", is_org_admin=True, manageable=[])}

    def resolver(_request):
        return admin["who"]

    app = FastAPI()
    app.include_router(build_router(tmp_path, admin_resolver=resolver))
    c = TestClient(app)
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


def test_hub_admin_cannot_grant_manage_access(client):
    hub = client.hub
    # demote to a hub admin (not org)
    client.admin["who"] = PortalAdmin(subject="ha", is_org_admin=False, manageable=[hub])
    r = client.post("/portal/api/access/grant",
                    json={"subject": "x", "hub": hub, "permission": "manage_access"})
    assert r.status_code == 403
    # but can grant a normal permission in their hub
    r = client.post("/portal/api/access/grant",
                    json={"subject": "x", "hub": hub, "permission": "prod_in"})
    assert r.status_code == 200


def test_hub_admin_cannot_touch_other_hub(client):
    client.admin["who"] = PortalAdmin(subject="ha", is_org_admin=False, manageable=["finance"])
    r = client.post("/portal/api/access/grant",
                    json={"subject": "x", "hub": "ops", "permission": "prod_in"})
    assert r.status_code == 403


def test_overview_and_workflows_and_audit(client):
    client.post("/portal/api/access/grant",
                json={"subject": "alice", "hub": client.hub, "permission": "prod_in"})
    ov = client.get("/portal/api/overview").json()
    assert ov["authoritative"] is True and ov["grants"] >= 1
    assert "workflows" in client.get("/portal/api/workflows").json()
    assert "rows" in client.get("/portal/api/audit").json()
