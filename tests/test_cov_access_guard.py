"""The one access decision for tools and MCP (hubzoid.access.guard): the
surface gate first, then the store; a blocked person holds nothing; an error
reading the store denies (fail closed); an embedding app's caller is judged on
the app's limit. Every MCP call is checked: a refused call never reaches the
server and is recorded, an allowed one is passed through, a failure is raised
as it was, and a server is wrapped only once."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hubzoid import access
from hubzoid.access import evaluator, guard


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_RESTRICTED_SURFACES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    access._stores.clear()
    h = tmp_path / "finance"
    h.mkdir()
    (h / "AGENTS.md").write_text("---\nname: finance\n---\nx")
    return h


def who(user, surface="web", app=None):
    return access.Identity.make(user, (), surface=surface, app=app)


def test_the_store_decides_and_a_block_holds_nothing(hub):
    gs = access.store_for(hub)
    gs.grant("ana@x.org", "finance", "ledger", actor="t")
    assert guard.decide(hub, who("ana@x.org"), "ledger")[0] is True
    assert guard.decide(hub, who(None), "ledger") == (False, "anonymous")      # the surface gate comes first
    assert guard.decide(hub, who("ben@x.org"), "ledger")[0] is False
    gs.suspend("ana@x.org", actor="t")
    assert guard.decide(hub, who("ana@x.org"), "ledger") == (False, "blocked")


def test_a_store_error_denies(hub, monkeypatch):
    def broken(hub_dir):
        raise RuntimeError("database gone")

    monkeypatch.setattr(access, "store_for", broken)
    assert guard.decide(hub, who("ana@x.org"), "ledger") == (False, "store-error")


def test_an_app_caller_is_judged_on_the_app(hub, monkeypatch):
    app = access.identity.AppScope("app-1", frozenset({"finance"}), frozenset({"lookup"}))
    monkeypatch.setattr(evaluator, "app_access", lambda hub_dir, a, permission, tool=None: SimpleNamespace(
        allowed=tool == "lookup", reason="outside_app"))
    caller = who("cy@x.org", surface="api", app=app)                 # no grant of her own in the store
    assert guard.decide(hub, caller, "ledger", tool="lookup") == (True, "app")
    assert guard.decide(hub, caller, "ledger", tool="pay") == (False, "outside_app")


class Server:
    name = "erp"

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    async def call_tool(self, tool_name, arguments, *a, **kw):
        self.calls.append((tool_name, arguments))
        if self.fail:
            raise self.fail
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="done")], isError=False)


def test_every_mcp_call_is_checked(hub, monkeypatch):
    recorded = []
    monkeypatch.setattr(guard, "server_source", lambda hub_dir, name: ("erp", None))
    monkeypatch.setattr(guard.audit_log, "tool_call", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(guard.audit_log, "finished", lambda where, result, **kw: recorded.append(dict(where, ok=True)))
    allow = {"value": False}
    monkeypatch.setattr(evaluator, "check_run", lambda hub_dir, ident, **kw: SimpleNamespace(
        allowed=allow["value"], reason="no_grant", **{"permission": "erp_write"}))

    server = Server()
    checked = guard.checked_server(server, hub)
    assert guard.checked_server(checked, hub) is checked                 # wrapped once
    refused = asyncio.run(checked.call_tool("post_invoice", {"amount": 5}))
    assert refused.isError is True and server.calls == []
    assert recorded[-1]["decision"] == "deny" and recorded[-1]["tool"] == "post_invoice"

    allow["value"] = True
    out = asyncio.run(checked.call_tool("post_invoice", {"amount": 5}))
    assert out.content[0].text == "done" and server.calls == [("post_invoice", {"amount": 5})]
    assert recorded[-1].get("ok") is True

    failing = guard.checked_server(Server(fail=TimeoutError("slow")), hub)
    with pytest.raises(TimeoutError):
        asyncio.run(failing.call_tool("post_invoice", {}))
    assert recorded[-1]["outcome"] == "error" and recorded[-1]["error"] == "TimeoutError"
