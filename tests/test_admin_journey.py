"""Acceptance: two distinct hubs, a grant-less hub, and three user roles."""


import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from hubzoid import deployment, db
from hubzoid.access import store_for, audit
from hubzoid.access import migrate
from hubzoid.access.store import GrantStore
from hubzoid.cli import app as cli
from hubzoid.portal import build_router, PortalAdmin


@pytest.fixture
def deployment_client(tmp_path, monkeypatch):
    for key in ("HUBZOID_OPERATIONAL_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(key, raising=False)
    dirs = []
    for name, perm in [
        ("finance", "ledger"),
        ("ops", "inventory"),
        ("new", "onboarding"),
    ]:
        p = tmp_path / name
        (p / "restricted").mkdir(parents=True)
        (p / "restricted" / f"{perm}.py").write_text("# permission declaration")
        w = p / "workflows" / "daily"
        w.mkdir(parents=True)
        (w / "main.py").write_text(
            'from hubzoid import workflow\n@workflow("daily 06:00")\ndef daily():\n    return "ok"\n'
        )
        dirs.append(p)
    deployment.save(
        tmp_path / "gateway" / "deployment.json",
        hubs=[
            dict(key=p.name, name=p.name.title(), path=str(p), model_id=p.name)
            for p in dirs
        ],
        operational_url=f"sqlite:///{tmp_path}/ops.db",
        owui_url="http://owui",
        owui_db=str(tmp_path / "owui.db"),
    )
    gs = store_for(dirs[0])
    gs.bootstrap(["root"])
    for p in dirs:
        gs.set_authoritative(True, hub=p.name)
    gs.grant("ha", "ops", "manage_access")
    gs.grant("alice", "finance", "ledger")
    role = {"user": PortalAdmin("root", True, [])}
    a = FastAPI()
    a.include_router(build_router(dirs[0], lambda request: role["user"]))
    c = TestClient(a)
    c.headers["origin"] = "http://testserver"
    return c, gs, role, dirs


def test_multi_hub_permissions_and_grantless_catalog(deployment_client):
    c, gs, role, dirs = deployment_client
    assert {h["key"] for h in c.get("/portal/api/hubs").json()["hubs"]} == {
        "finance",
        "ops",
        "new",
    }
    ps = c.get("/portal/api/permissions?hub=ops").json()["permissions"]
    assert {p["permission"] for p in ps} == {"inventory", "manage_access", "use_hub"}
    assert (
        c.post(
            "/portal/api/access/grant",
            json=dict(subject="alice", hub="ops", permission="ledger"),
        ).status_code
        == 422
    )
    assert (
        c.post(
            "/portal/api/access/grant",
            json=dict(subject="alice", hub="ops", permission="inventory"),
        ).status_code
        == 200
    )
    assert gs.can("alice", "ops", "inventory")
    assert not gs.can("alice", "ops", "ledger")
    workflows = c.get("/portal/api/workflows").json()["workflows"]
    assert {(w["hub"], w["name"]) for w in workflows} == {
        (p.name, "daily") for p in dirs
    }


def test_hub_admin_audit_and_account_isolation(deployment_client):
    c, gs, role, dirs = deployment_client
    for p in dirs:
        audit.record(
            p,
            user=p.name + "-user",
            surface="owui",
            tool="secret",
            decision="allow",
            reason="grant",
        )
    role["user"] = PortalAdmin("ha", False, ["ops"])
    assert {r["hub"] for r in c.get("/portal/api/audit").json()["rows"]} == {"ops"}
    assert c.get("/portal/api/audit?hub=finance").status_code == 403
    assert c.get("/portal/api/runs?hub=finance").status_code == 403
    assert all(
        r["hub"] == "ops" for r in c.get("/portal/api/access-changes").json()["rows"]
    )
    assert "alice" not in {
        p["subject"] for p in c.get("/portal/api/people").json()["people"]
    }
    assert (
        c.post("/portal/api/people/block", json={"subject": "alice"}).status_code == 403
    )
    role["user"] = None
    for endpoint in ("/hubs", "/people", "/audit", "/workflows", "/access-changes"):
        assert c.get("/portal/api" + endpoint).status_code == 403


def test_cli_and_bridges_use_same_deployment(deployment_client):
    c, gs, role, dirs = deployment_client
    r = CliRunner().invoke(cli, ["grant", "bob", "inventory", str(dirs[1])])
    assert r.exit_code == 0, r.output
    assert gs.can("bob", "ops", "inventory")
    assert db.operational_url(dirs[0]) == db.operational_url(dirs[1])
    assert deployment.owui_url(dirs[1]) == "http://owui"


def test_offboarding_denies_public_and_keeps_admin(deployment_client):
    c, gs, role, dirs = deployment_client
    gs.grant("*", "finance", "use_hub")
    assert (
        c.post("/portal/api/people/block", json={"subject": "root"}).status_code == 409
    )
    assert (
        c.post("/portal/api/people/block", json={"subject": "alice"}).status_code == 200
    )
    assert not gs.can("alice", "finance", "use_hub")
    assert not gs.permissions_for("alice", "finance")
    assert (
        c.post(
            "/portal/api/people/block", json={"subject": "alice", "suspended": False}
        ).status_code
        == 200
    )
    assert gs.can("alice", "finance", "use_hub")  # public, not restored direct grants
    assert not gs.can("alice", "finance", "ledger")


def test_migration_current_owui_schema_preserves_tools_and_denials(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/owui.db")
    with eng.begin() as c:
        for sql in [
            "CREATE TABLE user (id TEXT,email TEXT,role TEXT)",
            'CREATE TABLE "group" (id TEXT,name TEXT)',
            "CREATE TABLE group_member (group_id TEXT,user_id TEXT)",
            "CREATE TABLE model (id TEXT,user_id TEXT)",
            "CREATE TABLE access_grant (resource_type TEXT,resource_id TEXT,principal_type TEXT,principal_id TEXT,permission TEXT)",
        ]:
            c.execute(text(sql))
        c.execute(
            text(
                "INSERT INTO user VALUES ('a','a@x','user'),('b','b@x','user'),('c','c@x','user')"
            )
        )
        c.execute(text("INSERT INTO \"group\" VALUES ('g','ledger')"))
        c.execute(text("INSERT INTO group_member VALUES ('g','a'),('g','b')"))
        c.execute(text("INSERT INTO model VALUES ('finance','a')"))
        c.execute(
            text(
                "INSERT INTO access_grant VALUES ('model','finance','user','a','read')"
            )
        )
    plan = migrate.plan_from_owui(
        eng, "finance", model_id="finance", permissions=["ledger"]
    )
    gs = GrantStore(create_engine(f"sqlite:///{tmp_path}/ops.db"))
    before = gs.snapshot(["finance"])
    migrate.apply(gs, plan)
    assert gs.can("a@x", "finance", "ledger")
    assert not gs.can("b@x", "finance", "use_hub")  # tool group must not open model
    assert not gs.can("c@x", "finance", "use_hub")
    assert not migrate.effective_diff(gs, plan)
    assert gs.read_access_audit()
    gs.restore(before, actor="operator")
    assert not gs.is_authoritative("finance")
    assert not gs.list_grants("finance")
    assert any(r["action"] == "rollback" for r in gs.read_access_audit())


def test_all_grant_paths_have_history_and_identity(tmp_path):
    gs = GrantStore(create_engine(f"sqlite:///{tmp_path}/ops.db"))
    gs.bootstrap(["root"])
    gs.grant_many([("alice", "finance", "ledger")])
    assert gs.identity("alice")
    events = gs.read_access_audit()
    assert {r["actor"] for r in events} >= {"bootstrap", "bulk-import"}


def test_workflow_dry_run_never_imports_or_executes_code(tmp_path):
    w = tmp_path / "workflows" / "example"
    w.mkdir(parents=True)
    sentinel = tmp_path / "EXECUTED"
    (w / "main.py").write_text(
        f'from pathlib import Path\nPath({str(sentinel)!r}).touch()\nfrom hubzoid import workflow\n@workflow("daily 06:00")\ndef example(): return 1\n'
    )
    r = CliRunner().invoke(
        cli, ["schedule", "run", str(tmp_path), "example", "--dry-run"]
    )
    assert r.exit_code == 0, r.output
    assert not sentinel.exists()
    assert not (tmp_path / ".hubzoid" / "dbos.db").exists()
    r = CliRunner().invoke(
        cli, ["schedule", "run", str(tmp_path), "example", "--model", "x"]
    )
    assert r.exit_code == 2
    assert not sentinel.exists()


def test_bad_workflow_schedule_visible_without_import(tmp_path):
    from hubzoid.workflows.observe import definitions

    w = tmp_path / "workflows" / "broken"
    w.mkdir(parents=True)
    (w / "main.py").write_text('@workflow("every 0 minutes")\ndef broken(): pass\n')
    assert definitions(tmp_path)[0]["error"]


def test_sync_replaces_acl_with_empty_after_last_revoke(deployment_client, monkeypatch):
    from contextlib import contextmanager
    from hubzoid.access.reconcile import sync_owui
    import hubzoid.access.owui as owui

    c, gs, role, dirs = deployment_client
    writes = []

    def handler(req):
        if req.url.path == "/api/v1/users/":
            return httpx.Response(
                200,
                json={"users": [dict(id="a", email="alice", role="user")], "total": 1},
            )
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": req.url.params["id"],
                    "name": "Agent",
                    "params": {},
                    "meta": {},
                },
            )
        import json

        writes.append(json.loads(req.content))
        return httpx.Response(200, json={})

    @contextmanager
    def client(_):
        with httpx.Client(
            base_url="http://owui", transport=httpx.MockTransport(handler)
        ) as x:
            yield x

    monkeypatch.setattr(owui, "client_for", client)
    assert sync_owui(dirs[0])["state"] == "ok"
    assert next(w for w in writes if w["id"] == "finance")["access_grants"]
    gs.revoke("alice", "finance", "use_hub")
    writes.clear()
    assert sync_owui(dirs[0])["state"] == "ok"
    assert next(w for w in writes if w["id"] == "finance")["access_grants"] == []


def test_real_dbos_run_history_and_duplicate_dispatch(tmp_path):
    import subprocess
    import sys

    w = tmp_path / "workflows" / "daily"
    w.mkdir(parents=True)
    (w / "main.py").write_text("""from hubzoid import workflow, step, hub
@step()
def count():
    n=hub.state.get("count",0)+1
    hub.state["count"]=n
    return n
@workflow("every 2 minutes")
def daily():
    return count()
""")
    script = """
import os
from pathlib import Path
p=Path(%r)
os.environ['HUBZOID_OPERATIONAL_DB']='sqlite:///'+str(p/'ops.db')
os.environ['HUBZOID_DBOS_DB']='sqlite:///'+str(p/'dbos.db')
from hubzoid.workflows import runtime
from hubzoid.workflows.observe import runs
runtime.init(p)
runtime.load_workflows(p)
runtime.launch()
a=runtime.start('daily',scheduled_at='2026-01-01T00:02:00+00:00')
b=runtime.start('daily',scheduled_at='2026-01-01T00:02:00+00:00')
assert a.get_workflow_id()==b.get_workflow_id()
assert a.get_result()==1 and b.get_result()==1
rows=runs(p,name='daily')
assert len(rows)==1, rows
assert rows[0]['status']=='SUCCESS', rows
assert rows[0]['output']=='1', rows
assert runs(p,run_id=rows[0]['id'])[0]['steps']
print('HISTORY_AND_DEDUP_OK')
""" % str(tmp_path)
    p = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=90
    )
    assert p.returncode == 0 and "HISTORY_AND_DEDUP_OK" in p.stdout, p.stdout + p.stderr


def test_edge_partial_migration_and_navigation(deployment_client, monkeypatch):
    from hubzoid.edge import build_edge_app, EdgeRoute
    from hubzoid.portal_navigation import inject

    c, gs, role, dirs = deployment_client
    gs.set_authoritative(False, hub="ops")
    cfg = __import__("json").loads(
        (dirs[0] / ".hubzoid" / "deployment.json").read_text()
    )["manifest"]
    monkeypatch.setenv("HUBZOID_DEPLOYMENT", cfg)
    edge = build_edge_app(
        default_base="http://127.0.0.1:1",
        routes=[EdgeRoute("/portal", "http://127.0.0.1:2")],
    )
    with TestClient(edge) as x:
        assert (
            x.post(
                "/api/v1/models/model/update",
                json={"id": "finance", "access_grants": []},
            ).status_code
            == 403
        )
        assert (
            x.post(
                "/api/v1/models/model/update", json={"id": "ops", "access_grants": []}
            ).status_code
            != 403
        )
        assert (
            x.post("/api/v1/groups/create", json={"name": "old-team"}).status_code
            != 403
        )
        assert "Manage agent access" in x.get("/hubzoid-portal-navigation.js").text
    assert b"/hubzoid-portal-navigation.js" in inject(b"<html><body>Chat</body></html>")
