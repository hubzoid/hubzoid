"""A person an app vouched for, in the access checks (hubzoid.embed + hubzoid.access)."""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from hubzoid.access.evaluator import authorize, check_run
from hubzoid.access.identity import AppScope, Identity, identity_scope
from hubzoid.embed import config, scope
from hubzoid.embed.caller import AppCaller
from hubzoid.embed.config import Access


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.delenv("HUBZOID_DEPLOYMENT_FILE", raising=False)
    hub = tmp_path / "finance"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: finance\n---\nHelp.\n")
    _apps(tmp_path, '[erp_reads, "connector:erp", "mcp:odoo"]')
    return hub


def _apps(root, tools: str, *, enabled: bool = True):
    (root / "hubzoid.yaml").write_text(f"""version: 1
hubs: [finance]
apps:
  erpnext:
    secret_ref: env:ERP_APP_SECRET
    enabled: {'true' if enabled else 'false'}
    access: {{agents: [finance], tools: {tools}}}
    token_to: ["connector:erp", erp_reads]
""")
    stamp = time.time_ns() + 10**9
    os.utime(root / "hubzoid.yaml", ns=(stamp, stamp))
    config.reset()


def ident(tools=("erp_reads", "connector:erp", "mcp:odoo"), agents=("finance",)):
    return Identity.make("app_x", (), "app", app=AppScope("erpnext", frozenset(agents), frozenset(tools)))


def test_an_app_caller_is_judged_on_the_app_never_on_grants(hub):
    i = ident()
    assert authorize(hub, i, "use_hub", hub="finance").allowed
    assert not authorize(hub, i, "use_hub", hub="hr").allowed
    assert not authorize(hub, i, "manage_access", hub="finance").allowed   # a platform capability
    assert authorize(hub, i, "connector_erp", hub="finance").allowed
    assert not authorize(hub, i, "connector_gmail", hub="finance").allowed
    # Grants in the access store never reach them, even ones naming their subject.
    from hubzoid.access import store_for

    store_for(hub).grant("app_x", "hr", "use_hub", actor="test")
    store_for(hub).grant("app_x", "finance", "secret_reports", actor="test")
    assert not authorize(hub, i, "use_hub", hub="hr").allowed
    assert not authorize(hub, i, "secret_reports", hub="finance").allowed


def test_check_run_checks_each_tool(hub):
    i = ident()
    assert check_run(hub, i, tool="erp_reads").allowed
    assert check_run(hub, i, tool="delete_everything").reason == "app_tool"
    assert check_run(hub, i, tool="anything", server="odoo").allowed
    assert check_run(hub, i, tool="anything", server="github").reason == "app_tool"
    assert check_run(hub, i, tool="anything", connector="erp").allowed
    # Other callers are unchanged: no tool check for them.
    plain = Identity.make("ana@example.org", (), "web")
    assert check_run(hub, plain, tool="delete_everything").reason != "app_tool"


def test_the_apps_current_limit_wins_while_a_reply_runs(hub, tmp_path):
    i = ident()
    _apps(tmp_path, "[connector:erp]")
    assert check_run(hub, i, tool="erp_reads").reason == "app_tool"  # removed from the app
    _apps(tmp_path, '[erp_reads]', enabled=False)
    assert check_run(hub, i, tool="erp_reads").reason == "credential_ended"  # the run stops
    config.reset()
    (tmp_path / "hubzoid.yaml").write_text("version: 1\nhubs: [finance]\n")
    os.utime(tmp_path / "hubzoid.yaml", ns=(time.time_ns() + 2 * 10**9,) * 2)
    assert check_run(hub, i).reason == "credential_ended"  # the app is gone


def test_without_a_handed_over_caller_a_reply_may_do_nothing(hub):
    nobody = scope.run_identity(None, "app_x")
    assert not check_run(hub, nobody).allowed
    assert not check_run(hub, nobody, tool="erp_reads").allowed


def test_tool_lists_hide_tools_outside_the_limit(hub):
    from hubzoid.access.guard import visible_map

    tools = {n: SimpleNamespace(name=n, is_enabled=True) for n in ("erp_reads", "write_artifact")}
    with identity_scope(ident()):
        assert visible_map(tools) == {"erp_reads": True, "write_artifact": False}
    with identity_scope(Identity.make("ana@example.org", (), "web")):
        assert visible_map(tools) == {"erp_reads": True, "write_artifact": True}


def _caller(token="erp-token"):
    return AppCaller(app_id="erpnext", user_id="alice", subject="app_x", name="Alice", email="",
                     access=Access(frozenset({"finance"}), frozenset({"erp_reads", "connector:erp"})),
                     issued_at=0, expires_at=0, app_token=token)


def test_the_token_goes_only_where_the_app_says(hub):
    with scope.token_scope(hub, _caller()):
        assert scope.connector_headers("erp") == {"Authorization": "Bearer erp-token"}
        assert scope.connector_headers("gmail") is None
        assert scope.app_token() is None                  # no tool running
        with scope.tool_scope("erp_reads"):
            assert scope.app_token() == "erp-token"
        with scope.tool_scope("write_artifact"):
            assert scope.app_token() is None
        assert scope.scrub("auth failed for erp-token") == "auth failed for [redacted]"
    assert scope.app_token() is None and scope.connector_headers("erp") is None


def test_hand_over_is_once_and_only_for_the_owner():
    caller = _caller()
    scope.hand_over("c1", caller)
    assert scope.take("c1", "someone_else") is None
    scope.hand_over("c1", caller)
    assert scope.take("c1", "app_x") is caller
    assert scope.take("c1", "app_x") is None


def test_connectors_for_an_app_caller(hub, monkeypatch):
    from hubzoid.connectors import per_user, registry

    conn = SimpleNamespace(id="erp", enabled=True, url="https://erp.example.com/mcp", auth_type="oauth",
                           tool_allowlist=None)
    gmail = SimpleNamespace(id="gmail", enabled=True, url="https://g.example.com/mcp", auth_type="shared",
                            tool_allowlist=None)
    monkeypatch.setattr(registry, "offered_in", lambda hub_dir, hub: {"erp", "gmail"})
    monkeypatch.setattr(registry, "list_all", lambda hub_dir: [conn, gmail])
    monkeypatch.setattr(registry, "shared_headers", lambda hub_dir, cid: {"Authorization": "Bearer company"})
    with identity_scope(ident()), scope.token_scope(hub, _caller()):
        servers = per_user.per_user_servers(hub)
    assert [s.server_id for s in servers] == ["erp"]          # gmail is outside the limit
    assert servers[0].headers == {"Authorization": "Bearer erp-token"}
    assert "erp-token" not in repr(servers[0])


def test_the_claude_hook_names_server_and_connector(hub):
    from hubzoid import factory_claude

    seen = {}

    def fake_check(hub_dir, ident_, **kw):
        seen.update(kw)
        from hubzoid.access.evaluator import ALLOW

        return ALLOW

    import asyncio

    from hubzoid.access import evaluator

    import dataclasses

    @dataclasses.dataclass
    class Opts:
        hooks: dict
        mcp_servers: dict

    original = evaluator.check_run
    evaluator.check_run = fake_check
    try:
        servers = {"my_erp": {}, "odoo": {}, "odoo__admin": {}, "hubzoid__admin": {}}
        patched = factory_claude._identity_checked(Opts(hooks={}, mcp_servers=servers), hub, ident())
        hook = patched.hooks["PreToolUse"][0].hooks[0]
        asyncio.run(hook({"tool_name": "mcp__my_erp__list_orders"}, "id", None))
        assert seen == {"tool": "list_orders", "server": None, "connector": "erp"}
        seen.clear()
        asyncio.run(hook({"tool_name": "mcp__odoo__search"}, "id", None))
        assert seen == {"tool": "search", "server": "odoo", "connector": None}
        seen.clear()
        assert asyncio.run(hook({"tool_name": "mcp__hubzoid__read_knowledge"}, "id", None)) == {}  # own tools
        # Names two servers could own are refused for an app, whichever way round.
        for name in ("mcp__odoo__admin__drop", "mcp__hubzoid__admin__drop"):
            out = asyncio.run(hook({"tool_name": name}, "id", None))
            assert out["hookSpecificOutput"]["permissionDecision"] == "deny", name
        assert seen == {}
        asyncio.run(hook({"tool_name": "mcp__stranger__x"}, "id", None))          # unknown: never in a limit
        assert seen["server"] == "?unknown"
    finally:
        evaluator.check_run = original
