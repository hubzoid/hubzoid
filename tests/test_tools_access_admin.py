"""The management proposal tools (`HUBZOID_MANAGEMENT_TOOLS`).

They only propose; the actor is the request identity on every surface, never a
model argument; they refuse anonymous callers, scheduled work and Slack; and
they are off unless enabled and inert on an agent whose access is not managed.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext
from sqlalchemy import text

from hubzoid.access import Identity, identity_scope
from hubzoid.tools import access_admin, make_all

from tests.test_access_service import (  # noqa: F401 — shared fixture and helpers
    DELEGATE,
    ROOT,
    dep,
)


def _invoke(tool, raw: dict) -> str:
    args = json.dumps(raw)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args)
    return asyncio.run(tool.on_invoke_tool(ctx, args))


def _tools(dep, monkeypatch):
    monkeypatch.setenv("HUBZOID_MANAGEMENT_TOOLS", "true")
    return {t.name: t for t in access_admin.make(SimpleNamespace(hub_dir=dep.hub_dir))}


def _requests(dep):
    with dep.gs.engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT actor, surface, kind, target, status FROM hz_change_requests")).mappings()]


def test_off_by_default(dep, monkeypatch):
    monkeypatch.delenv("HUBZOID_MANAGEMENT_TOOLS", raising=False)
    assert access_admin.make(SimpleNamespace(hub_dir=dep.hub_dir)) == []


def test_registered_through_make_all(dep, monkeypatch):
    monkeypatch.setenv("HUBZOID_MANAGEMENT_TOOLS", "1")
    from hubzoid import settings

    ctx = SimpleNamespace(hub_dir=dep.hub_dir, output_dir=dep.hub_dir / "output",
                          session_id="s", settings=settings.load(dep.hub_dir),
                          skills=[], knowledge=[], delegates=[], connections=None)
    names = set(make_all(ctx))
    assert {"my_management_scope", "propose_access_change", "propose_new_account"} <= names


def test_no_actor_or_password_parameter(dep, monkeypatch):
    tools = _tools(dep, monkeypatch)
    for t in tools.values():
        props = set(t.params_json_schema.get("properties", {}))
        assert not props & {"actor", "as_user", "subject_of", "password", "manager"}
    assert set(tools["propose_new_account"].params_json_schema["properties"]) == {
        "email", "name", "hub", "grant"}


@pytest.mark.parametrize("surface", ["owui", "whatsapp", "mcp", "telegram", "web", "api"])
def test_proposes_on_allowed_surfaces(dep, monkeypatch, surface):
    tools = _tools(dep, monkeypatch)
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.com/b/finance")
    with identity_scope(Identity.make(DELEGATE, surface=surface)):
        assert tools["propose_access_change"].is_enabled()
        out = _invoke(tools["propose_access_change"],
                      {"person": "ann@x.org", "hub": "finance", "grant": ["ledger"]})
    assert out.startswith("Proposed, not applied"), out
    assert "https://hub.example.com/portal/#/confirm/" in out
    assert not dep.gs.can("ann@x.org", "finance", "ledger")
    assert _requests(dep) == [dict(actor=DELEGATE, surface=surface, kind="access",
                                   target="ann@x.org", status="pending")]
    if surface in ("whatsapp", "telegram"):
        assert "sign in" in out.lower()


@pytest.mark.parametrize("ident", [
    Identity(),  # anonymous
    Identity.make(DELEGATE, surface="slack"),
    Identity.make(DELEGATE, surface="slack-channel"),
    Identity.make(DELEGATE, surface="slack-dm"),
    Identity.make(DELEGATE, surface="workflow"),
    Identity.make(DELEGATE, surface="system"),
    Identity.make("workflow:close", surface="owui"),
    Identity.make("ann@x.org", surface="owui"),  # manages nothing
])
def test_refused_callers(dep, monkeypatch, ident):
    tools = _tools(dep, monkeypatch)
    with identity_scope(ident):
        for t in tools.values():
            assert t.is_enabled() is False
        out = _invoke(tools["propose_access_change"],
                      {"person": "ann@x.org", "hub": "finance", "grant": ["ledger"]})
        scope = _invoke(tools["my_management_scope"], {})
    assert out.startswith("[not proposed")
    assert scope.startswith("[not available")
    assert _requests(dep) == []


def test_model_cannot_name_the_actor(dep, monkeypatch):
    tools = _tools(dep, monkeypatch)
    with identity_scope(Identity.make(DELEGATE, surface="owui")):
        out = _invoke(tools["propose_access_change"], {
            "person": "ann@x.org", "hub": "ops", "grant": ["inventory"],
            "actor": ROOT, "as_user": ROOT})
    # ROOT could grant in ops; the delegate cannot, and the extra fields are ignored.
    assert out.startswith("[not proposed") or "error" in out.lower()
    assert all(r["actor"] == DELEGATE for r in _requests(dep))
    assert not any(r["target"] == "ann@x.org" and r["kind"] == "access" and r["actor"] == ROOT
                   for r in _requests(dep))


def test_ceiling_applies_to_proposals(dep, monkeypatch):
    tools = _tools(dep, monkeypatch)
    with identity_scope(Identity.make(DELEGATE, surface="owui")):
        out = _invoke(tools["propose_access_change"],
                      {"person": "ann@x.org", "hub": "finance", "grant": ["payroll"]})
        assert "Outside your access" in out
        scope = _invoke(tools["my_management_scope"], {})
    assert "finance: ledger, use_hub" in scope and "payroll" not in scope


def test_new_account_proposal_carries_no_password(dep, monkeypatch):
    tools = _tools(dep, monkeypatch)
    with identity_scope(Identity.make(ROOT, surface="mcp")):
        out = _invoke(tools["propose_new_account"],
                      {"email": "new@x.org", "name": "New", "hub": "ops"})
    assert out.startswith("Proposed"), out
    assert "password" not in out.lower() or "never" not in out.lower()
    assert dep.owui.by_email("new@x.org") is None  # nothing created yet
    with dep.gs.engine.connect() as c:
        plan = json.loads(c.execute(text("SELECT plan FROM hz_change_requests")).scalar())
    assert "password" not in plan


def test_inert_on_a_legacy_agent(dep, monkeypatch):
    tools = _tools(dep, monkeypatch)
    dep.gs.set_authoritative(False, hub="finance")
    with identity_scope(Identity.make(ROOT, surface="owui")):
        assert tools["propose_access_change"].is_enabled() is False
        out = _invoke(tools["propose_access_change"],
                      {"person": "ann@x.org", "hub": "ops", "grant": ["inventory"]})
    assert out.startswith("[not proposed") and "chat app" in out


def test_confirm_url(monkeypatch):
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.com/b/sales")
    assert access_admin.confirm_url("/portal/#/confirm/x") == "https://hub.example.com/portal/#/confirm/x"
    monkeypatch.delenv("HUBZOID_PUBLIC_URL")
    monkeypatch.setenv("WEBUI_URL", "https://chat.example.com/")
    assert access_admin.confirm_url("/p") == "https://chat.example.com/p"
    monkeypatch.delenv("WEBUI_URL")
    assert access_admin.confirm_url("/p") == "/p"
