"""Multi-hub gateway on SQLite: one SHARED operational DB (access + catalog),
per-bridge DBOS system DB."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine

import hubzoid.db as db
from hubzoid.access.store import MANAGE_ACCESS, ORG, GrantStore


def test_operational_url_precedence(tmp_path):
    hub = tmp_path / "hubA"
    # default: per-hub (standalone == the hub DB)
    assert db.operational_url(hub, env={}).endswith("hubA/.hubzoid/hub.db")
    # HUBZOID_OPERATIONAL_DB wins (a gateway's shared file)
    env = {"HUBZOID_OPERATIONAL_DB": "sqlite:////shared/ops.db"}
    assert db.operational_url(hub, env=env) == "sqlite:////shared/ops.db"
    # DATABASE_URL also shares
    assert db.operational_url(hub, env={"DATABASE_URL": "postgresql://x/y"}) == "postgresql://x/y"


def test_dbos_url_is_per_bridge_on_sqlite(tmp_path):
    a, b = tmp_path / "hubA", tmp_path / "hubB"
    # even with a shared operational DB, each bridge's DBOS DB is its own file
    env = {"HUBZOID_OPERATIONAL_DB": "sqlite:////shared/ops.db"}
    ua, ub = db.dbos_url(a, env=env), db.dbos_url(b, env=env)
    assert ua != ub and ua.endswith("hubA/.hubzoid/dbos.db")
    # a Postgres DATABASE_URL may be shared for DBOS (multi-instance)
    assert db.dbos_url(a, env={"DATABASE_URL": "postgresql://x/y"}) == "postgresql://x/y"


def test_bridges_share_grants_via_one_operational_db(tmp_path):
    shared = f"sqlite:///{tmp_path / 'ops.db'}"
    # two "bridges" open the same shared operational DB
    bridge_a = GrantStore(create_engine(shared))
    bridge_b = GrantStore(create_engine(shared))
    # an org grant written through bridge A is visible in hub B (bridge B)
    bridge_a.grant("root", ORG, MANAGE_ACCESS)
    assert bridge_b.can("root", "hubB", MANAGE_ACCESS)
    # per-hub authority set for hubA does NOT flip hubB
    bridge_a.set_authoritative(True, hub="hubA")
    assert bridge_b.is_authoritative("hubA") is True
    assert bridge_b.is_authoritative("hubB") is False


def test_workflow_catalog_shared(tmp_path):
    shared = f"sqlite:///{tmp_path / 'ops.db'}"
    a = GrantStore(create_engine(shared))
    b = GrantStore(create_engine(shared))
    a.publish_workflows("finance", [("review_prs", "every 2 minutes", "Asia/Kolkata")])
    b.publish_workflows("ops", [("nightly", "daily 02:00", None)])
    # the org portal (either bridge) sees BOTH hubs' workflows
    names = {(w["hub"], w["name"]) for w in a.list_workflows()}
    assert ("finance", "review_prs") in names and ("ops", "nightly") in names
    # a hub admin sees only theirs
    assert {w["hub"] for w in a.list_workflows(hubs=["ops"])} == {"ops"}
