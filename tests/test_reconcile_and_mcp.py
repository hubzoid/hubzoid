"""Visibility reconciler + the MCP use_hub front door."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine

import hubzoid.access as access
import hubzoid.db as db
from hubzoid.access.reconcile import sync, visibility_plan
from hubzoid.access.store import EVERYONE, USE_HUB, GrantStore


@pytest.fixture()
def store(tmp_path):
    return GrantStore(create_engine(f"sqlite:///{tmp_path / 'hub.db'}"))


def test_visibility_plan(store):
    store.grant("alice", "finance", "prod_in")   # implies use_hub
    store.grant("alice", "ops", USE_HUB)
    store.grant("bob", "finance", USE_HUB)
    store.grant(EVERYONE, "public", USE_HUB)
    store.grant("root", "*", "manage_access")     # org domain — excluded
    plan = visibility_plan(store)
    assert plan["alice"] == {"finance", "ops"}
    assert plan["bob"] == {"finance"}
    assert plan[EVERYONE] == {"public"}
    assert "root" not in plan                     # org domain not a visible hub


def test_sync_calls_projector(store):
    store.grant("alice", "finance", USE_HUB)
    store.grant("bob", "ops", USE_HUB)
    seen = {}

    def projector(subject, hubs):
        seen[subject] = hubs

    n = sync(store, projector)
    assert n == 2
    assert seen == {"alice": ["finance"], "bob": ["ops"]}


def test_sync_skips_failing_projection(store):
    store.grant("alice", "finance", USE_HUB)
    store.grant("bob", "ops", USE_HUB)

    def projector(subject, hubs):
        if subject == "alice":
            raise RuntimeError("owui down")

    n = sync(store, projector)   # bob still projected; alice logged + skipped
    assert n == 1


# --- MCP front door: can(use_hub) when authoritative ------------------------

def test_mcp_verifier_gates_on_use_hub(tmp_path, monkeypatch):
    hub_dir = tmp_path
    (hub_dir).mkdir(exist_ok=True)
    eng = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    access._stores.clear()
    gs = access.store_for(hub_dir)
    gs.set_authoritative(True)

    from hubzoid import mcp_server
    from hubzoid.access import owui_api_keys

    # any token resolves to this email
    monkeypatch.setattr(owui_api_keys, "resolve_email", lambda hd, tok: "alice@corp")
    verifier = mcp_server._build_verifier(hub_dir, access_group="ignored")

    # no use_hub -> denied
    assert asyncio.run(verifier.verify_token("t")) is None
    # grant use_hub in this hub -> allowed
    gs.grant("alice@corp", hub_dir.name, USE_HUB)
    tok = asyncio.run(verifier.verify_token("t"))
    assert tok is not None and tok.claims["email"] == "alice@corp"
