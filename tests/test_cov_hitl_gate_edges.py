"""The approval gate's wrappers when something goes wrong
(hubzoid.hitl.gate): a tool that is part of asking is never wrapped and a
tool is wrapped once; an MCP server that can't list its tools gives no
read-only hints (every tool counts as a write); a refused or waiting call
never reaches the server or the tool; Claude's hook denies with the reason;
a failing read-only hint, an unreadable rules file or a failing automatic
check all fall back to asking (or the stricter rule), never to running."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from hubzoid.hitl import gate, service

WAITING = service.Verdict("ask", request={"id": "req-1", "kind": "approval", "title": "Post the invoice",
                                          "requester_email": None})


@pytest.fixture
def verdicts(monkeypatch):
    """The gate's decision per tool name, and what it was asked."""
    seen, plan = [], {}

    def evaluate(hub_dir, tool, arguments, *, connector=None, read_only=None):
        seen.append({"tool": tool, "arguments": arguments, "connector": connector, "read_only": read_only})
        return plan.get(tool, service.Verdict("allow"))

    monkeypatch.setattr(gate, "evaluate", evaluate)
    monkeypatch.setattr(gate._request_ctx, "native_session", lambda: None)
    return SimpleNamespace(seen=seen, plan=plan)


class Server:
    name = "erp"

    def __init__(self, list_fails=False):
        self.calls, self.list_fails = [], list_fails

    async def list_tools(self):
        if self.list_fails:
            raise ConnectionError("listing failed")
        return [SimpleNamespace(name="read_ledger", annotations=SimpleNamespace(readOnlyHint=True))]

    async def call_tool(self, tool_name, arguments=None):
        self.calls.append(tool_name)
        return "ran"


def test_an_mcp_server_is_guarded_once_and_refusals_never_reach_it(tmp_path, verdicts):
    server = Server()
    guarded = gate.guard_mcp_server(server, tmp_path)
    assert gate.guard_mcp_server(guarded, tmp_path) is guarded
    assert asyncio.run(guarded.call_tool("read_ledger", {"q": 1})) == "ran"
    assert verdicts.seen[-1] == {"tool": "read_ledger", "arguments": {"q": 1}, "connector": "erp", "read_only": True}

    verdicts.plan["pay"] = service.Verdict("refuse", message="Payments are not allowed here.")
    refused = asyncio.run(guarded.call_tool("pay"))
    assert refused.content[0].text == "Payments are not allowed here." and refused.isError is False

    verdicts.plan["post_invoice"] = WAITING
    waiting = json.loads(asyncio.run(guarded.call_tool("post_invoice", {})).content[0].text)
    assert waiting["status"] == "waiting" and waiting["request_id"] == "req-1"
    assert "the person's approval" in waiting["message"] and server.calls == ["read_ledger"]


def test_a_server_that_cant_list_tools_counts_every_tool_as_a_write(tmp_path, verdicts):
    guarded = gate.guard_mcp_server(Server(list_fails=True), tmp_path)
    asyncio.run(guarded.call_tool("read_ledger", {}))
    assert verdicts.seen[-1]["read_only"] is None


def test_exempt_and_wrapped_tools_are_left_alone(tmp_path):
    ask = SimpleNamespace(name="ask_question", on_invoke_tool=None)
    assert gate.wrap(ask, tmp_path) is ask
    done = SimpleNamespace(name="post_invoice", on_invoke_tool=None, hitl_wrapped=True)
    assert gate.wrap(done, tmp_path) is done


def hook_call(hook, name, args=None):
    return asyncio.run(hook({"tool_name": name, "tool_input": args or {}}, "use-1", None))


def test_claudes_hook_denies_with_the_reason(tmp_path, verdicts, monkeypatch):
    monkeypatch.setattr(gate, "_asks_for_writes", lambda hub_dir, tool, connector: True)

    async def broken_hint(server, tool):
        raise TimeoutError("server slow")

    hook = gate.claude_hook(tmp_path, "hubzoid", read_only=broken_hint)
    assert hook_call(hook, "Bash") == {} and hook_call(hook, "mcp__hubzoid__save_note") == {}
    assert hook_call(hook, "mcp__github__list_issues") == {}
    assert verdicts.seen[-1]["read_only"] is None and verdicts.seen[-1]["connector"] == "github"

    verdicts.plan["delete_repo"] = service.Verdict("refuse", message="Deleting repositories is not allowed.")
    out = hook_call(hook, "mcp__github__delete_repo")["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"] == "Deleting repositories is not allowed."
    verdicts.plan["create_issue"] = WAITING
    out = hook_call(hook, "mcp__github__create_issue", {"title": "x"})["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and json.loads(out["permissionDecisionReason"])["request_id"] == "req-1"


def test_an_unreadable_rules_file_and_unknown_servers_fall_back(tmp_path, monkeypatch):
    from hubzoid.hitl import config as cfglib

    def unreadable(hub_dir):
        raise cfglib.ConfigError("bad", path="/", line=1)

    monkeypatch.setattr(cfglib, "load", unreadable)
    assert gate.mcp_parts(tmp_path, "mcp__erp__post_invoice") == ("erp", "post_invoice")
    assert gate._asks_for_writes(tmp_path, "post_invoice", "erp") is False   # the rule can't be read: ask anyway

    import hubzoid.personal_mcp as personal
    from hubzoid.connectors import registry

    def boom(*a, **kw):
        raise RuntimeError("no servers")

    monkeypatch.setattr(personal, "_hub_server_keys", boom)
    monkeypatch.setattr(registry, "list_all", boom)
    # No known servers: the first separator ends the server's key.
    assert gate.mcp_parts(tmp_path, "mcp__my_acme__delete__order") == ("my_acme", "delete__order")


def test_the_automatic_check_asks_a_person_when_it_fails(tmp_path, monkeypatch):
    from hubzoid import jev

    def no_key(*a, **kw):
        raise RuntimeError("no Jev key")

    monkeypatch.setattr(jev, "call", no_key)
    assert gate._auto_check(tmp_path)("delete_all", {}) is False
    monkeypatch.setattr(jev, "call", lambda *a, **kw: {"answers": {"permission": {"choice": "caution"}}})
    assert gate._auto_check(tmp_path)("delete_all", {}) is False
