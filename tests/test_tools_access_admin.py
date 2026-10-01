"""The access tools: three reads and two proposals.

They need the `access_tools` grant ("Manage access from chat") in the agent
where the chat runs plus management rights, or the deprecated
`HUBZOID_MANAGEMENT_TOOLS=true` (legacy mode: every manager, no grant). They
only propose; the actor is the request identity on every surface, never a
model argument; they refuse anonymous callers, scheduled work and Slack; what
they read stays inside the caller's management scope; and they are inert on an
agent whose access is not managed. Tests that set the legacy flag keep the
1.0.x behaviour covered.
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext
from sqlalchemy import text

from hubzoid import config_secrets
from hubzoid.access import Identity, identity_scope
from hubzoid.access.guard import visible
from hubzoid.tools import access_admin, make_all

from tests.test_access_service import (  # noqa: F401 — shared fixture and helpers
    DELEGATE,
    ROOT,
    _unavailable,
    actor,
    dep,
)

ALL = ["my_management_scope", "who_has_access", "explain_access", "propose_access_change",
       "propose_new_account"]


def _invoke(tool, raw: dict) -> str:
    args = json.dumps(raw)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args)
    return asyncio.run(tool.on_invoke_tool(ctx, args))


def _tools(dep, monkeypatch):
    """Legacy mode: every manager, no grant."""
    monkeypatch.setenv("HUBZOID_MANAGEMENT_TOOLS", "true")
    return {t.name: t for t in access_admin.make(SimpleNamespace(hub_dir=dep.hub_dir))}


def _granted(dep, monkeypatch, *holders, hub="finance"):
    """Grant mode (no flag): `holders` get access_tools in `hub` from an
    organization administrator. The tools run in finance."""
    for key in ("HUBZOID_MANAGEMENT_TOOLS", "HUBZOID_ACCESS_TOOLS"):
        monkeypatch.delenv(key, raising=False)
    for who in holders:
        dep.svc.apply_access_change(actor(ROOT), who, hub, [("grant", "access_tools")])
    return {t.name: t for t in access_admin.make(SimpleNamespace(hub_dir=dep.hub_dir))}


def _requests(dep):
    with dep.gs.engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT actor, surface, kind, target, status FROM hz_change_requests")).mappings()]


def test_hidden_until_granted(dep, monkeypatch):
    """No flag: the tools exist but nobody sees or can use them without the grant."""
    tools = _granted(dep, monkeypatch)
    assert list(tools) == ALL
    for who in (ROOT, DELEGATE):
        with identity_scope(Identity.make(who, surface="owui")):
            assert [visible(t) for t in tools.values()] == [False] * len(ALL)
            assert not any(t.is_enabled() for t in tools.values())
            read = _invoke(tools["who_has_access"], {"hub": "finance"})
            out = _invoke(tools["propose_access_change"],
                          {"person": "ann@x.org", "hub": "finance", "grant": ["ledger"]})
        assert read == f"[not available: {access_admin.NOT_GRANTED}]"
        assert out == f"[not proposed: {access_admin.NOT_GRANTED}]"
    assert _requests(dep) == []


def test_visible_to_a_manager_with_the_grant(dep, monkeypatch):
    tools = _granted(dep, monkeypatch, DELEGATE, "ann@x.org")
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.com/b/finance")
    with identity_scope(Identity.make(DELEGATE, surface="mcp")):
        assert all(visible(t) and t.is_enabled() for t in tools.values())
        out = _invoke(tools["propose_access_change"],
                      {"person": "bob@x.org", "hub": "finance", "grant": ["ledger"]})
    assert out.startswith("Proposed, not applied"), out
    # A manager without the grant, and a grant holder who manages nothing.
    for who in (ROOT, "ann@x.org"):
        with identity_scope(Identity.make(who, surface="owui")):
            assert not any(visible(t) or t.is_enabled() for t in tools.values()), who
    with identity_scope(Identity.make("ann@x.org", surface="owui")):
        assert _invoke(tools["who_has_access"], {"hub": "finance"}) == (
            "[not available: You don't manage access to any agent.]")


def test_the_grant_counts_only_in_the_agent_where_the_chat_runs(dep, monkeypatch):
    tools = _granted(dep, monkeypatch, ROOT, hub="ops")
    with identity_scope(Identity.make(ROOT, surface="owui")):
        assert not any(visible(t) for t in tools.values())


@pytest.mark.parametrize("switch", ["HUBZOID_ACCESS_TOOLS", "HUBZOID_MANAGEMENT_TOOLS"])
def test_kill_switches_remove_the_tools(dep, monkeypatch, switch):
    monkeypatch.setattr(config_secrets, "_base_env", None)  # deployment layer = os.environ
    monkeypatch.setenv(switch, "false")
    assert access_admin.make(SimpleNamespace(hub_dir=dep.hub_dir)) == []
    row = {e["permission"]: e for e in dep.svc.catalog("finance")}["access_tools"]
    assert (row["available"], row["status"]) == (False, "Disabled for this hub")


def test_legacy_flag_warns_once(dep, monkeypatch, caplog):
    monkeypatch.setattr(access_admin, "_legacy_warned", False)
    with caplog.at_level(logging.WARNING, logger="hubzoid.tools.access_admin"):
        _tools(dep, monkeypatch)
        _tools(dep, monkeypatch)
    assert sum("deprecated" in r.getMessage() for r in caplog.records) == 1


def test_catalogue_row(dep):
    row = {e["permission"]: e for e in dep.svc.catalog("finance")}["access_tools"]
    assert (row["label"], row["group"], row["section"], row["sensitive"],
            row["delegate_grantable"], row["surfaces"]) == (
        "Manage access from chat", "tools", "access", True, False, ["chat", "mcp"])


def test_registered_through_make_all(dep, monkeypatch):
    monkeypatch.setenv("HUBZOID_MANAGEMENT_TOOLS", "1")
    from hubzoid import settings

    ctx = SimpleNamespace(hub_dir=dep.hub_dir, output_dir=dep.hub_dir / "output",
                          session_id="s", settings=settings.load(dep.hub_dir),
                          skills=[], knowledge=[], delegates=[], connections=None)
    names = set(make_all(ctx))
    assert set(ALL) <= names


def test_no_actor_or_password_parameter(dep, monkeypatch):
    tools = _tools(dep, monkeypatch)
    for t in tools.values():
        props = set(t.params_json_schema.get("properties", {}))
        assert not props & {"actor", "as_user", "subject_of", "password", "manager"}
    assert set(tools["propose_new_account"].params_json_schema["properties"]) == {
        "email", "name", "hub", "grant"}
    assert set(tools["who_has_access"].params_json_schema["properties"]) == {"hub"}
    assert set(tools["explain_access"].params_json_schema["properties"]) == {"person", "hub"}


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
@pytest.mark.parametrize("mode", ["legacy", "granted"])
def test_refused_callers(dep, monkeypatch, ident, mode):
    tools = (_tools(dep, monkeypatch) if mode == "legacy" else
             _granted(dep, monkeypatch, DELEGATE, "ann@x.org", "workflow:close"))
    with identity_scope(ident):
        for t in tools.values():
            assert t.is_enabled() is False
        out = _invoke(tools["propose_access_change"],
                      {"person": "ann@x.org", "hub": "finance", "grant": ["ledger"]})
        scope = _invoke(tools["my_management_scope"], {})
        read = _invoke(tools["explain_access"], {"person": "ann@x.org"})
    assert out.startswith("[not proposed")
    assert scope.startswith("[not available") and read.startswith("[not available")
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
    # Console labels, with the id a proposal takes.
    assert "finance: Ledger (ledger), Use this agent (use_hub)" in scope and "payroll" not in scope


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



def test_runtimes_that_list_tools_themselves_hide_them_from_non_managers(dep, monkeypatch):
    """Claude and Codex do not evaluate is_enabled; they ask access.guard.visible,
    which must honour this check too (release review: non-managers saw them)."""
    from hubzoid.access.guard import visible

    tools = _tools(dep, monkeypatch)
    for who, shown in ((ROOT, True), (DELEGATE, True), ("stranger@x.org", False)):
        with identity_scope(Identity.make(who, surface="owui")):
            assert [visible(t) for t in tools.values()] == [shown] * len(tools), who


# ---- the read tools ---------------------------------------------------------------

def test_who_has_access_stays_in_the_delegates_agents(dep, monkeypatch):
    gs = dep.gs
    gs.grant("ann@x.org", "finance", "ledger", actor="test")
    gs.upsert_identity(email="ann@x.org", owui_id="u-ann", display="Ann A", pending=True)
    gs.grant("workflow:close", "finance", "ledger", actor="test")
    gs.grant("cy@x.org", "ops", "inventory", actor="test")
    tools = _granted(dep, monkeypatch, DELEGATE)
    with identity_scope(Identity.make(DELEGATE, surface="owui")):
        out = _invoke(tools["who_has_access"], {"hub": "finance"})
        other = _invoke(tools["who_has_access"], {"hub": "ops"})
        unknown = _invoke(tools["who_has_access"], {"hub": "nope"})
    lines = out.splitlines()
    assert lines[:2] == ["finance: 4 people and services have access.",
                         "Not open to everyone signed in."]
    assert "- Ann A <ann@x.org>: Ledger (ledger), Use this agent (use_hub) [awaiting approval]" in lines
    assert f"- {ROOT}: no grants in this agent [awaiting sign-up, organization administrator]" in lines
    assert ("- workflow:close: Ledger (ledger), Use this agent (use_hub) [service identity]"
            in lines)
    assert any(line.startswith(f"- {DELEGATE}: ") and "Manage access from chat (access_tools)" in line
               for line in lines)
    assert "cy@x.org" not in out and "ops" not in out
    assert other == "[not available: Cannot manage ops]"
    assert unknown == "[not available: Cannot manage nope]"


def test_who_has_access_reports_everyone_and_caps_rows(dep, monkeypatch):
    gs = dep.gs
    for i in range(60):
        gs.grant(f"u{i:02d}@x.org", "finance", "use_hub", actor="test")
    gs.grant("*", "finance", "use_hub", actor="test", carry_over_public=True)
    gs.upsert_identity(email="dan@x.org", owui_id="u-dan", display="Dan")
    gs.grant("gone@x.org", "finance", "use_hub", actor="test")
    _unavailable(gs, "gone@x.org")
    tools = _granted(dep, monkeypatch, ROOT)
    with identity_scope(Identity.make(ROOT, surface="mcp")):
        out = _invoke(tools["who_has_access"], {"hub": "finance"})
    lines = out.splitlines()
    total = 60 + 3  # plus ROOT, the delegate and gone@x.org; everyone is not a row
    assert lines[0] == f"finance: {total} people and services have access."
    assert lines[1] == "Everyone signed in can use this agent; 1 of them has no access of their own here."
    assert sum(line.startswith("- ") for line in lines) == 50
    assert lines[-1] == f"... and {total - 50} more, see the Console."
    assert "- gone@x.org: Use this agent (use_hub) [awaiting sign-up, account unavailable]" in lines


def test_explain_access_gives_the_source(dep, monkeypatch):
    gs = dep.gs
    gs.grant("ann@x.org", "finance", "ledger", actor="test")
    gs.upsert_identity(email="ann@x.org", owui_id="u-ann", display="Ann A")
    gs.grant("*", "finance", "use_hub", actor="test", carry_over_public=True)
    gs.grant("gone@x.org", "finance", "use_hub", actor="test")
    gs.upsert_identity(email="gone@x.org", owui_id="u-gone", display="Gone")
    _unavailable(gs, "gone@x.org")
    tools = _granted(dep, monkeypatch, ROOT)
    with identity_scope(Identity.make(ROOT, surface="owui")):
        ann = _invoke(tools["explain_access"], {"person": "ann@x.org"})
        root = _invoke(tools["explain_access"], {"person": ROOT, "hub": "ops"})
        gone = _invoke(tools["explain_access"], {"person": "gone@x.org", "hub": "finance"})
        nobody = _invoke(tools["explain_access"], {"person": "zed@x.org", "hub": "ops"})
        public = _invoke(tools["explain_access"], {"person": "zed@x.org"})
    assert ann.splitlines() == [
        "Ann A <ann@x.org>. Account: active.",
        "In finance:",
        "- Ledger (ledger): direct grant",
        "- Use this agent (use_hub): direct grant, everyone signed in",
    ]
    assert root.splitlines() == [
        f"{ROOT}. Account: no chat account yet (awaiting sign-up).",
        "Organization administrator: manages access in every agent.",
        "In ops:",
        "- Manage access (manage_access): organization administrator",
    ]
    assert "Account: unavailable in the chat app." in gone
    assert "They can use none of this while their chat account is unavailable." in gone
    assert nobody == "zed@x.org has no access to ops."
    assert "- Use this agent (use_hub): everyone signed in" in public and "ops" not in public


def test_explain_access_stays_in_scope(dep, monkeypatch):
    gs = dep.gs
    gs.grant("cy@x.org", "ops", "inventory", actor="test")
    gs.upsert_identity(email="cy@x.org", owui_id="u-cy", display="Cy Ops")
    tools = _granted(dep, monkeypatch, DELEGATE)
    with identity_scope(Identity.make(DELEGATE, surface="owui")):
        cy = _invoke(tools["explain_access"], {"person": "cy@x.org"})
        ops = _invoke(tools["explain_access"], {"person": "cy@x.org", "hub": "ops"})
        mine = _invoke(tools["explain_access"], {"person": DELEGATE})
    # Nothing about other agents, nor the account of someone outside their agents.
    assert cy == "cy@x.org has no access in the agents you manage."
    assert ops == "[not available: Cannot manage ops]"
    assert "In finance:" in mine and "In ops" not in mine
    assert "- Manage access from chat (access_tools): direct grant" in mine


def test_explain_access_never_describes_an_outsider(dep, monkeypatch):
    gs = dep.gs
    gs.grant("carol@x.org", "ops", "inventory", actor="test")
    gs.upsert_identity(email="carol@x.org", owui_id="u-carol", display="Carol Secret")
    gs.grant("*", "finance", "use_hub", actor="test", carry_over_public=True)
    tools = _granted(dep, monkeypatch, DELEGATE)
    with identity_scope(Identity.make(DELEGATE, surface="owui")):
        out = _invoke(tools["explain_access"], {"person": "carol@x.org"})
    assert out == ("carol@x.org has no access of their own in the agents you manage. "
                   "Everyone signed in can use: finance.")
    assert "Carol" not in out and "Account" not in out


def test_explain_access_hub_is_optional_in_the_schema(dep, monkeypatch):
    tools = _tools(dep, monkeypatch)
    assert tools["explain_access"].params_json_schema.get("required") == ["person"]


def test_explain_access_does_not_call_a_private_agent_public(dep, monkeypatch):
    dep.gs.grant("boss@x.org", "*", "manage_access", actor="test")
    tools = _granted(dep, monkeypatch, DELEGATE)
    with identity_scope(Identity.make(DELEGATE, surface="owui")):
        out = _invoke(tools["explain_access"], {"person": "boss@x.org"})
    assert out == "boss@x.org has no access in the agents you manage."
