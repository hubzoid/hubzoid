"""End-to-end demonstration of the manual upgrade path for a controlled deployment:
seed an Open WebUI model, migrate it (the explicit plan_from_owui + apply cutover),
and show the dashboard API then manages that agent's access — allowed/denied preserved,
edits work — and that an operator rollback returns it to legacy read-only. This is the
"workable path to the dashboard", exercised against real code (not the browser fixture)."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import hubzoid.access as access
import hubzoid.db as db
from hubzoid.access import migrate
from hubzoid.portal import PortalAdmin, build_router

REAL_OWUI = {
    "samarth_diamond": Path(
        "/Users/shreyarao/Desktop/WaveAssist/Hubzoid/SamarthDiamond/SamarthDiamondHub/.openwebui-data/webui.db"
    ),
    "samarth_jewellery": Path(
        "/Users/shreyarao/Desktop/WaveAssist/Hubzoid/SamarthJewellery/SamarthJewelleryHub/.openwebui-data/webui.db"
    ),
}


def _seed_owui(path: Path, users, grants):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE user (id TEXT PRIMARY KEY, email TEXT, role TEXT)")
    con.execute("CREATE TABLE model (id TEXT PRIMARY KEY, user_id TEXT, is_active INTEGER, access_control TEXT)")
    con.execute('CREATE TABLE "group" (id TEXT PRIMARY KEY, name TEXT)')
    con.execute("CREATE TABLE group_member (group_id TEXT, user_id TEXT)")
    con.execute("CREATE TABLE access_grant (resource_type TEXT, resource_id TEXT, principal_type TEXT, principal_id TEXT, permission TEXT)")
    for u in users:
        con.execute("INSERT INTO user VALUES (?,?,?)", u)
    con.execute("INSERT INTO model VALUES ('m1','uadmin',1,NULL)")
    for g in grants:
        con.execute("INSERT INTO access_grant VALUES (?,?,?,?,?)", g)
    con.commit()
    con.close()


@pytest.fixture()
def deployment(tmp_path, monkeypatch):
    d = tmp_path / "finance"
    (d / "restricted").mkdir(parents=True)
    (d / "restricted" / "ledger.py").write_text("# restricted tool\n")
    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    access._stores.clear()
    _seed_owui(
        d / ".openwebui-data" / "webui.db",
        [("uadmin", "admin@fin.io", "admin"), ("uann", "ann@fin.io", "user"), ("udan", "dan@fin.io", "user")],
        [("model", "m1", "user", "uann", "read")],
    )
    admin = {"who": PortalAdmin(subject="admin@fin.io", is_org_admin=True, manageable=[])}
    app = FastAPI()
    app.include_router(build_router(d, admin_resolver=lambda _r: admin["who"]))
    c = TestClient(app)
    c.headers.update({"origin": "http://testserver"})
    c.hub = d.name
    c.dir = d
    c.gs = access.store_for(d)
    return c


def test_manual_migration_makes_the_agent_dashboard_managed(deployment):
    c = deployment
    gs = c.gs
    # Before migration: legacy, read-only in the dashboard.
    assert c.get("/portal/api/access", params={"hub": c.hub}).json()["editable"] is False

    # The manual cutover: establish the dashboard admin, then migrate from OWUI.
    gs.bootstrap(["admin@fin.io"], authoritative=False)
    source = create_engine(f"sqlite:///{(c.dir / '.openwebui-data' / 'webui.db').resolve()}")
    try:
        plan = migrate.plan_from_owui(source, c.hub, model_id="m1", permissions=["use_hub", "ledger"])
    finally:
        source.dispose()
    snapshot = gs.snapshot([c.hub])  # backup for rollback
    migrate.apply(gs, plan, authoritative=True)

    # After migration: dashboard-managed. Allowed preserved, denied preserved.
    acc = c.get("/portal/api/access", params={"hub": c.hub}).json()
    assert acc["editable"] is True and acc["authoritative"] is True
    assert gs.can("ann@fin.io", c.hub, "use_hub")       # allowed
    assert not gs.can("dan@fin.io", c.hub, "use_hub")   # denied
    assert gs.can("admin@fin.io", "*", "manage_access") # dashboard admin

    # A dashboard edit now takes effect through the API.
    r = c.post("/portal/api/access/grant",
               json={"subject": "dan@fin.io", "hub": c.hub, "permission": "use_hub"})
    assert r.status_code == 200
    assert gs.can("dan@fin.io", c.hub, "use_hub")

    # Operator rollback returns it to legacy; a fresh store (restart) stays rolled back.
    gs.restore(json.loads(json.dumps(snapshot)), actor="operator-rollback")
    access._stores.clear()
    acc2 = c.get("/portal/api/access", params={"hub": c.hub}).json()
    assert acc2["editable"] is False and acc2["authoritative"] is False
    assert c.post("/portal/api/access/grant",
                  json={"subject": "x@fin.io", "hub": c.hub, "permission": "use_hub"}).status_code == 409


@pytest.mark.parametrize("name,src", list(REAL_OWUI.items()))
def test_rehearse_migration_against_real_customer_owui_copy(tmp_path, name, src):
    """Rehearsal against a PROTECTED COPY of the real customer OWUI database (never the
    original). Both currently have no registered OWUI model for the hub, so the explicit
    migration must FAIL CLEARLY (MigrationBlocked) before changing any permission — the
    exact prerequisite to surface: register the hub's OWUI model first."""
    if not src.is_file():
        pytest.skip(f"real OWUI db not available: {src}")
    copy = tmp_path / "webui.db"
    shutil.copy2(src, copy)  # protected copy; original untouched
    engine = create_engine(f"sqlite:///{copy}")
    try:
        with pytest.raises(migrate.MigrationBlocked):
            migrate.plan_from_owui(engine, name, permissions=["use_hub"])
    finally:
        engine.dispose()
    assert src.is_file()  # original customer DB untouched


def test_verification_must_use_pre_cutover_baseline_not_reread_after_sync(tmp_path, monkeypatch):
    """Review #2: `access sync` rewrites OWUI model visibility (public → explicit
    per-user grants). Re-running `diff --from-owui` AFTER sync rebuilds its baseline
    from that mutated source and can report a false mismatch even though Hubzoid's
    grants are unchanged. Verification must compare against the pre-cutover baseline
    (diff before sync). This reproduces zero-diff before projection, nonzero after."""
    d = tmp_path / "finance"
    (d / "restricted").mkdir(parents=True)
    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    access._stores.clear()
    owui = d / ".openwebui-data" / "webui.db"
    # A PUBLIC model (an explicit '*' read grant): signed-in users may enter.
    _seed_owui(owui, [("uadmin", "admin@fin.io", "admin"), ("uann", "ann@fin.io", "user")],
               [("model", "m1", "user", "*", "read")])
    gs = access.store_for(d)
    gs.bootstrap(["admin@fin.io"], authoritative=False)

    def plan():
        src = create_engine(f"sqlite:///{owui.resolve()}")
        try:
            return migrate.plan_from_owui(src, d.name, model_id="m1", permissions=["use_hub"])
        finally:
            src.dispose()

    baseline = plan()
    migrate.apply(gs, baseline, authoritative=True)
    # Verify BEFORE projection against the same baseline: zero diff.
    assert migrate.diff(gs, baseline) == {"missing": [], "extra": []}
    grants_after_apply = sorted(gs.list_grants(d.name))

    # Simulate what `access sync` does to OWUI: replace the public '*' grant with an
    # explicit per-user grant. Hubzoid's store is NOT touched.
    with sqlite3.connect(owui) as con:
        con.execute("DELETE FROM access_grant WHERE principal_id='*'")
        con.execute("INSERT INTO access_grant VALUES ('model','m1','user','uann','read')")
        con.commit()
    assert sorted(gs.list_grants(d.name)) == grants_after_apply  # store unchanged

    # Re-reading the plan from the now-mutated OWUI yields a DIFFERENT baseline, so a
    # post-sync from-owui diff falsely reports a mismatch.
    post_sync = migrate.diff(gs, plan())
    assert post_sync["missing"] or post_sync["extra"], "expected a false mismatch after sync-style change"
