"""End-to-end access enforcement against the real HubzoidTestHub.

Model-free: builds the hub's actual restricted tool through the guard and drives
the decision by grants in the Casbin store. Proves the whole access path — the
surface gate in front, then can() — on a real hub's `restricted/` folder.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine

import hubzoid.access as access
import hubzoid.db as db
from hubzoid.access.identity import Identity, identity_scope

TEST_HUB = Path(__file__).resolve().parents[2] / "HubzoidTestHub" / "test-hub"

pytestmark = pytest.mark.skipif(
    not (TEST_HUB / "restricted" / "testers.py").exists(),
    reason="HubzoidTestHub not present",
)


@pytest.fixture()
def guarded_tool(tmp_path, monkeypatch):
    """The real `testers_secret` tool, guarded, over a scratch access DB made
    authoritative (so the guard consults Casbin, not legacy groups)."""
    eng = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    access._stores.clear()
    gs = access.store_for(TEST_HUB)
    gs.set_authoritative(True)
    guarded = access.apply(TEST_HUB, {})
    assert "testers_secret" in guarded, "restricted tool not loaded"
    return guarded["testers_secret"], gs


def _invoke(tool, ident: Identity) -> str:
    async def _call():
        return await tool.on_invoke_tool(None, "{}")

    with identity_scope(ident):
        return asyncio.run(_call())


def _enabled(tool, ident: Identity) -> bool:
    """The guard's is_enabled gate for `ident` (the access decision, without
    invoking the underlying SDK tool)."""
    with identity_scope(ident):
        return bool(tool.is_enabled(None, None))


def test_denied_without_grant(guarded_tool):
    tool, _gs = guarded_tool
    ident = Identity.make("alice", surface="owui")
    assert _enabled(tool, ident) is False
    # invoking anyway returns the denial (the wall behind is_enabled)
    out = _invoke(tool, ident)
    assert "access denied" in out.lower()


def test_allowed_with_grant(guarded_tool):
    tool, gs = guarded_tool
    ident = Identity.make("alice", surface="owui")
    assert _enabled(tool, ident) is False
    gs.grant("alice", "test-hub", "testers")
    assert _enabled(tool, ident) is True   # the grant opens the door


def test_surface_gate_beats_grant(guarded_tool):
    tool, gs = guarded_tool
    gs.grant("alice", "test-hub", "testers")
    # a shared-channel surface is denied before can() is ever consulted
    assert _enabled(tool, Identity.make("alice", surface="slack-channel")) is False
    out = _invoke(tool, Identity.make("alice", surface="slack-channel"))
    assert "access denied" in out.lower()


def test_use_hub_implied_by_tool_grant(guarded_tool):
    _tool, gs = guarded_tool
    gs.grant("alice", "test-hub", "testers")
    assert gs.can("alice", "test-hub", "use_hub")   # implication held end-to-end
