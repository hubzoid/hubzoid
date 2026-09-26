"""'Everyone signed in' (`*` with use_hub): existing grants keep working and can
be removed; no path creates a new one, except migration carrying over
demonstrably public legacy access.

No model and no network: the two-hub SQLite deployment from test_access_service.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from hubzoid.access import migrate
from hubzoid.access.service import Denied, plan_hash
from hubzoid.access.store import EVERYONE, USE_HUB, BroadAccessRefused, GrantStore
from hubzoid.cli import app as cli
from hubzoid.portal import build_router

from tests.test_access_service import (  # noqa: F401 — shared fixture and helpers
    DELEGATE,
    ROOT,
    actor,
    dep,
)


@pytest.fixture
def api(dep, monkeypatch):
    monkeypatch.setenv("HUBZOID_PORTAL_DEV", "1")
    app = FastAPI()
    app.include_router(build_router(dep.hub_dir))
    client = TestClient(app)

    def as_(subject):
        monkeypatch.setenv("HUBZOID_PORTAL_DEV_USER", subject)
        client.headers.update({"origin": "http://testserver"})
        return client

    dep.as_ = as_
    return dep


def _everyone_rows(gs):
    return [g for g in gs.list_grants() if g[0] == EVERYONE]


def test_existing_grant_is_enforced_shown_and_removable_by_org_admins_only(api):
    gs = api.gs
    gs.grant(EVERYONE, "finance", USE_HUB, actor="migration", carry_over_public=True)
    gs.upsert_identity(email="walk.in@x.org", owui_id="u-walk")      # relies on it alone
    gs.upsert_identity(email="ann@x.org", owui_id="u-ann")
    gs.grant("ann@x.org", "finance", "ledger", actor="test")          # has a named grant
    assert gs.can("walk.in@x.org", "finance", USE_HUB)

    body = api.as_(ROOT).get("/portal/api/access", params={"hub": "finance"}).json()
    assert body["public"] is True and body["public_reliant"] == 1
    everyone = next(r for r in body["rows"] if r["subject"] == EVERYONE)
    assert everyone["perms"] == [USE_HUB] and everyone["status"] == "everyone"

    payload = dict(subject=EVERYONE, hub="finance", permission=USE_HUB)
    assert api.as_(DELEGATE).post("/portal/api/access/revoke", json=payload).status_code == 403
    assert _everyone_rows(gs)
    assert api.as_(ROOT).post("/portal/api/access/revoke", json=payload).status_code == 200
    assert not _everyone_rows(gs)
    assert not gs.can("walk.in@x.org", "finance", USE_HUB)
    assert gs.can("ann@x.org", "finance", "ledger")  # named grants are untouched


def test_no_path_creates_a_new_everyone_grant(api, monkeypatch):
    gs, svc = api.gs, api.svc
    # The service, for organization administrators too.
    for who in (ROOT, DELEGATE):
        with pytest.raises(Denied) as e:
            svc.apply_access_change(actor(who), EVERYONE, "finance", [("grant", USE_HUB)])
        assert (e.value.status, e.value.code) == (403, "no_new_everyone")
    # The management API.
    client = api.as_(ROOT)
    grant = dict(subject=EVERYONE, hub="finance", permission=USE_HUB)
    assert client.post("/portal/api/access/grant", json=grant).status_code == 403
    assert client.post("/portal/api/access/apply", json=dict(
        subject=EVERYONE, hub="finance",
        operations=[dict(action="grant", permission=USE_HUB)])).status_code == 403
    # Proposals from the management tools, and a confirmation of one that
    # somehow exists.
    with pytest.raises(Denied) as e:
        svc.propose(actor(ROOT, surface="whatsapp", via="bridge"),
                    dict(kind="access", hub="finance", subject=EVERYONE, grant=[USE_HUB]))
    assert e.value.code == "no_new_everyone"
    plan = dict(kind="access", hub="finance", subject=EVERYONE, grant=[USE_HUB], revoke=[])
    rid, now = "req_everyone_0123456789", time.time()
    with gs.engine.begin() as c:
        c.execute(text(
            "INSERT INTO hz_change_requests (id, created, expires, actor, surface, kind, hub, "
            "target, plan, plan_hash, status) VALUES (:id, :c, :e, :a, 'whatsapp', 'access', "
            "'finance', '*', :p, :h, 'pending')"),
            dict(id=rid, c=now, e=now + 600, a=ROOT, p=json.dumps(plan, sort_keys=True,
                 separators=(",", ":")), h=plan_hash(rid, ROOT, plan)))
    with pytest.raises(Denied) as e:
        svc.confirm(actor(ROOT), rid, plan_hash=plan_hash(rid, ROOT, plan))
    assert e.value.code == "no_new_everyone"
    # The CLI, with a message that names the alternative.
    monkeypatch.chdir(api.hub_dir)
    r = CliRunner().invoke(cli, ["grant", "*", "use_hub", "--hub", "finance", str(api.hub_dir)])
    assert r.exit_code == 1 and "named people" in r.output
    # The store itself, on every write path, without the migration keyword.
    with pytest.raises(BroadAccessRefused):
        gs.grant(EVERYONE, "finance", USE_HUB)
    with pytest.raises(BroadAccessRefused):
        gs.apply_changes(EVERYONE, "finance", [("grant", USE_HUB)])
    with pytest.raises(BroadAccessRefused):
        gs.grant_many([(EVERYONE, "finance", USE_HUB)])
    with pytest.raises(BroadAccessRefused):
        gs.apply_migration([(EVERYONE, "finance", USE_HUB)], [], ["finance"])
    assert not _everyone_rows(gs)


def test_migration_carries_over_public_legacy_access_and_says_so(tmp_path):
    store = GrantStore(create_engine(f"sqlite:///{tmp_path / 'hub.db'}"))
    plan = migrate.MigrationPlan()
    plan.add_grant(EVERYONE, "publichub", USE_HUB)
    plan.add_grant("ann@x.org", "publichub", "ledger")
    assert any(w.startswith("Everyone signed in (carried over): publichub") for w in plan.warnings)
    migrate.apply(store, plan, authoritative=True)
    assert store.can("anyone-signed-in", "publichub", USE_HUB)
    # Removing it afterwards stays possible.
    store.revoke(EVERYONE, "publichub", USE_HUB, actor="cli:test")
    assert not store.can("anyone-signed-in", "publichub", USE_HUB)


def test_fresh_hubs_have_no_everyone_grant(tmp_path):
    store = GrantStore(create_engine(f"sqlite:///{tmp_path / 'hub.db'}"))
    store.bootstrap(["root@x.org"], authoritative=True, hub="fresh")
    store.provision_owner("owner@x.org", "fresh", fresh=True)
    assert not _everyone_rows(store)
    assert not store.can("walk.in@x.org", "fresh", USE_HUB)
