"""The `call_jev` chat tool: one gated tool on all three runtimes.

Built through the real factories (OpenAI Agents SDK, Claude Agent SDK, Codex
app-server) on an authoritative hub with Console-style grants, with OpenRouter
replaced by a local fake. It is disabled until `jev` is granted: callers without
the grant do not see it on any runtime and cannot invoke it, including by name.
Callers with it get the same tool name, schema and result everywhere, for each
question type and a combined request, attributed to them in usage and audit.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from sqlalchemy import create_engine, text

from hubzoid import _request_ctx
from hubzoid.access import Identity, identity_scope, store_for
from hubzoid.tools import call_jev as jev_tool

KEY = "sk-or-v1-chat-tool-SECRET-5555"
ALLOWED, PLAIN, MANAGER = "ana@example.org", "ben@example.org", "cy@example.org"
TOOL = "call_jev"

NOUL = {"is_billing": {"type": "noul", "instructions": "Is this a billing problem?"}}
CHOICE = {"route": {"type": "choice", "instructions": "Which team?",
                    "criteria": {"billing": "charges", "technical_support": "errors"}}}
SCORE = {"urgency": {"type": "score", "instructions": "How urgent?", "criteria": ["low", "medium", "high"]}}
ALL = {**NOUL, **CHOICE, **SCORE}


def _answer(q):
    if q["type"] == "noul":
        return {"type": "noul", "noul": 0.93}
    if q["type"] == "choice":
        first = next(iter(q["criteria"]))
        return {"type": "choice", "choice": first, "confidence": 0.9,
                "probabilities": {k: (0.9 if k == first else 0.1 / (len(q["criteria"]) - 1))
                                  for k in q["criteria"]}}
    top = len(q["criteria"]) - 1
    return {"type": "score", "score": float(top), "confidence": 0.8,
            "probabilities": {str(i): (1.0 if i == top else 0.0) for i in range(top + 1)}}


@pytest.fixture
def jev_http(monkeypatch):
    """A fake Decisions API answering whatever it is asked. Returns the requests."""
    sent = []

    def fake_post(url, json=None, timeout=None, headers=None):
        sent.append({"url": url, "body": json, "auth": headers.get("Authorization")})
        return httpx.Response(200, json={
            "id": f"gen-dec-{len(sent)}", "model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
            "answers": {n: _answer(q) for n, q in json["questions"].items()},
            "usage": {"input_tokens": 300, "output_tokens": 20, "cost": 0.0000126}})

    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


@pytest.fixture
def hub(tmp_path, monkeypatch):
    d = tmp_path / "support"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: support\ndescription: Support desk\n---\nHelp the support team.\n")
    for k in ("HUBZOID_DEPLOYMENT", "DATABASE_URL", "OPENROUTER_API_KEY", "HUBZOID_BROWSER",
              "HUBZOID_RESTRICTED_SURFACES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("JEV_OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    gs = store_for(d)
    gs.set_authoritative(True, hub="support")
    gs.grant(ALLOWED, "support", "jev", actor="test")
    gs.grant(PLAIN, "support", "use_hub", actor="test")
    gs.grant(MANAGER, "support", "manage_access", actor="test")
    return d


def _who(user, surface="owui"):
    return Identity.make(user, surface=surface) if user else Identity.make(None)


CALLERS = [(ALLOWED, "owui", True), (ALLOWED, "api", True), (ALLOWED, "slack", False),
           (PLAIN, "owui", False), (MANAGER, "owui", False), (None, "owui", False)]
CALLER_IDS = ["granted", "granted-api", "granted-on-slack", "use_hub-only", "manage_access-only", "anonymous"]


# --- the three runtimes ------------------------------------------------------------
def _openai_tools(hub):
    from agents import RunContextWrapper

    from hubzoid import factory

    agent = factory.build_agent(hub, model_override="openai/gpt-4o-mini")
    return {t.name: t for t in asyncio.run(agent.get_all_tools(RunContextWrapper(context=None)))}


def _claude_server(options):
    return options.mcp_servers["hubzoid"]["instance"]


async def _claude_list(server):
    from mcp.types import ListToolsRequest

    r = await server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    return {t.name: t for t in r.root.tools}


async def _claude_call(server, name, args):
    from mcp.types import CallToolRequest, CallToolRequestParams

    r = await server.request_handlers[CallToolRequest](
        CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=args)))
    return r.root.content[0].text if r.root.content else ""


def _codex_exchange(rt, args):
    from tests.test_codex_runtime import Process, events

    async def run():
        proc = Process(events({"method": "item/tool/call", "id": 10, "params": {
            "tool": TOOL, "arguments": args, "callId": "c1", "threadId": "thread"}}))
        await _drain(rt._exchange(proc, "decide this", "/tmp", {}))
        return proc

    proc = asyncio.run(run())
    start = next(m for m in proc.messages if m.get("method") == "thread/start")
    reply = next(m for m in proc.messages if m.get("id") == 10)
    return {t["name"]: t for t in start["params"]["dynamicTools"]}, reply["result"]["contentItems"][0]["text"]


async def _drain(gen):
    return [x async for x in gen]


# --- disabled by default -------------------------------------------------------------
def test_the_capability_is_in_the_console_catalog(hub):
    from hubzoid.deployment import permission_catalog

    jev = next(p for p in permission_catalog(hub) if p["permission"] == "jev")
    assert jev["label"] == "Call Jev"
    assert "JEV_OPENROUTER_API_KEY" in jev["description"]


def test_entry_and_access_management_do_not_include_it(hub):
    gs = store_for(hub)
    assert gs.can(ALLOWED, "support", "jev")
    assert gs.can(ALLOWED, "support", "use_hub")       # the grant includes entry
    assert not gs.can(PLAIN, "support", "jev")
    assert not gs.can(MANAGER, "support", "jev")


@pytest.mark.parametrize("user,surface,allowed", CALLERS, ids=CALLER_IDS)
def test_openai_agents_shows_it_only_to_granted_callers(hub, user, surface, allowed):
    with identity_scope(_who(user, surface)):
        names = _openai_tools(hub)
    assert (TOOL in names) is allowed
    assert "read_knowledge" in names                    # ungated tools are unaffected


@pytest.mark.parametrize("user,surface,allowed", CALLERS, ids=CALLER_IDS)
def test_claude_shows_it_only_to_granted_callers(hub, user, surface, allowed):
    from hubzoid.factory_claude import build_claude_runtime

    rt = build_claude_runtime(hub)
    with identity_scope(_who(user, surface)):
        opts = rt._options_for_turn()
        listed = asyncio.run(_claude_list(_claude_server(opts)))
    assert (TOOL in listed) is allowed
    assert (f"mcp__hubzoid__{TOOL}" in opts.allowed_tools) is allowed
    assert "read_knowledge" in listed and "remember" not in listed   # remember needs curator


@pytest.mark.parametrize("user,surface,allowed", CALLERS, ids=CALLER_IDS)
def test_codex_shows_it_only_to_granted_callers(hub, jev_http, user, surface, allowed):
    from hubzoid.factory_codex import build_codex_runtime

    rt = build_codex_runtime(hub)
    rt.tool_mode = "off"
    with identity_scope(_who(user, surface)):
        offered, reply = _codex_exchange(rt, {"state": "charged twice", "questions": NOUL})
    assert (TOOL in offered) is allowed
    if allowed:
        assert json.loads(reply)["answers"]["is_billing"]["noul"] == 0.93
    else:
        assert reply == "Tool is not available in this hub." and "jev" not in reply
        assert jev_http == []


def test_naming_the_hidden_tool_does_not_run_it(hub, jev_http, tmp_path):
    """Claude: the turn's server has no such tool. The boot-time server (reached
    any other way) still refuses at the guard and logs the attempt."""
    from hubzoid.factory_claude import build_claude_runtime

    rt = build_claude_runtime(hub)
    args = {"state": "charged twice", "questions": NOUL}
    with identity_scope(_who(PLAIN)):
        per_turn = asyncio.run(_claude_call(_claude_server(rt._options_for_turn()), TOOL, args))
        direct = asyncio.run(_claude_call(_claude_server(rt._options), TOOL, args))
    assert "0.93" not in per_turn
    assert direct.startswith(f"[access denied: '{TOOL}' requires the 'jev' permission")
    assert jev_http == []
    with create_engine(f"sqlite:///{tmp_path / 'ops.db'}").connect() as c:
        rows = c.execute(text("SELECT subject, tool, decision FROM hz_access_decisions")).fetchall()
    assert (PLAIN, TOOL, "deny") in [tuple(r) for r in rows]


def test_legacy_hub_uses_the_jev_group(tmp_path, monkeypatch, jev_http):
    from hubzoid.factory_claude import build_claude_runtime

    d = tmp_path / "legacy"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: legacy\ndescription: d\n---\nbody\n")
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'legacy-ops.db'}")
    monkeypatch.setenv("JEV_OPENROUTER_API_KEY", KEY)
    rt = build_claude_runtime(d)
    for groups, allowed in ((["jev"], True), (["sales"], False)):
        with identity_scope(Identity.make("dee@example.org", groups, surface="owui")):
            listed = asyncio.run(_claude_list(_claude_server(rt._options_for_turn())))
        assert (TOOL in listed) is allowed


# --- the same tool everywhere ------------------------------------------------------
def test_same_name_schema_and_result_on_every_runtime(hub, jev_http):
    from hubzoid.factory_claude import build_claude_runtime
    from hubzoid.factory_codex import build_codex_runtime

    args = {"state": "Nobody can log in and orders are blocked.", "questions": ALL}
    with identity_scope(_who(ALLOWED)):
        oa = _openai_tools(hub)[TOOL]
        oa_result = asyncio.run(oa.on_invoke_tool(None, json.dumps(args)))
        server = _claude_server(build_claude_runtime(hub)._options_for_turn())
        cl = asyncio.run(_claude_list(server))[TOOL]
        cl_result = asyncio.run(_claude_call(server, TOOL, args))
        rt = build_codex_runtime(hub)
        rt.tool_mode = "off"
        offered, cx_result = _codex_exchange(rt, args)
    cx = offered[TOOL]
    assert oa.name == cl.name == cx["name"] == TOOL
    assert oa.description == cl.description == cx["description"] == jev_tool.DESCRIPTION
    assert oa.params_json_schema == cl.inputSchema == cx["inputSchema"] == jev_tool.PARAMS_SCHEMA
    assert oa_result == cl_result == cx_result
    assert set(json.loads(oa_result)["answers"]) == {"is_billing", "route", "urgency"}


@pytest.mark.parametrize("questions", [NOUL, CHOICE, SCORE, ALL], ids=["noul", "choice", "score", "combined"])
def test_each_question_type_in_chat(hub, jev_http, questions):
    from hubzoid.factory_claude import build_claude_runtime

    with identity_scope(_who(ALLOWED)):
        server = _claude_server(build_claude_runtime(hub)._options_for_turn())
        out = json.loads(asyncio.run(_claude_call(server, TOOL, {"state": "a ticket", "questions": questions})))
    assert out["model"] == "typesafe/jev-1.13-20260917"
    assert set(out["answers"]) == set(questions)
    for name, q in questions.items():
        assert out["answers"][name]["type"] == q["type"]
    assert jev_http[0]["body"]["questions"] == questions
    assert jev_http[0]["auth"] == f"Bearer {KEY}"


def test_usage_and_audit_name_the_person_surface_and_chat(hub, jev_http, tmp_path):
    from hubzoid.factory_claude import build_claude_runtime

    with identity_scope(_who(ALLOWED)), _request_ctx.chat_scope("chat-77"):
        server = _claude_server(build_claude_runtime(hub)._options_for_turn())
        asyncio.run(_claude_call(server, TOOL, {"state": "a ticket", "questions": NOUL}))
    with create_engine(f"sqlite:///{tmp_path / 'ops.db'}").connect() as c:
        usage = c.execute(text("SELECT kind, surface, subject, chat_id, model, cost_usd, status "
                               "FROM hz_usage")).fetchall()
        audit = c.execute(text("SELECT subject, surface, tool, decision FROM hz_access_decisions")).fetchall()
    assert [tuple(r) for r in usage] == [
        ("jev", "web", ALLOWED, "chat-77", "typesafe/jev-1.13-20260917", 0.0000126, "ok")]
    assert (ALLOWED, "owui", TOOL, "allow") in [tuple(r) for r in audit]


# --- readable failures, no key -----------------------------------------------------------
def _call_as_allowed(hub, raw_args: str):
    ft = jev_tool.make(type("Ctx", (), {"hub_dir": hub})())[0]
    with identity_scope(_who(ALLOWED)):
        return asyncio.run(ft.on_invoke_tool(None, raw_args))


@pytest.mark.parametrize("setup,args,expected", [
    ("no-key", {"state": "x", "questions": NOUL}, "Jev needs JEV_OPENROUTER_API_KEY"),
    ("ok", {"state": "x", "questions": {"q": {"type": "choice", "instructions": "x", "options": {}}}},
     "unknown field(s) options"),
    ("ok", {"state": "", "questions": NOUL}, "state must be non-empty"),
    ("401", {"state": "x", "questions": NOUL}, "OpenRouter rejected JEV_OPENROUTER_API_KEY (HTTP 401)"),
    ("empty", {"state": "x", "questions": NOUL}, "no noul answer for question 'is_billing'"),
    ("boom", {"state": "x", "questions": NOUL}, "unexpected KeyError; see the hub log"),
    ("ok", "not json", "the arguments are not valid JSON"),
    ("ok", ["a list"], "must be an object with state and questions"),
], ids=["no-key", "bad-question", "empty-state", "401", "empty-reply", "unexpected", "bad-json", "not-object"])
def test_failures_are_readable_and_never_show_the_key(hub, monkeypatch, caplog, setup, args, expected):
    caplog.set_level(logging.DEBUG)
    if setup == "no-key":
        monkeypatch.delenv("JEV_OPENROUTER_API_KEY")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-chat-key-not-for-jev")
    replies = {"401": httpx.Response(401, json={"error": {"code": 401, "message": f"User not found. {KEY}"}}),
               "empty": httpx.Response(200, json={"answers": {}})}

    def fake_post(url, json=None, timeout=None, headers=None):
        if setup == "boom":
            raise KeyError("upstream")
        return replies[setup]

    monkeypatch.setattr(httpx, "post", fake_post)
    raw = args if isinstance(args, str) else json.dumps(args)
    out = _call_as_allowed(hub, raw)
    assert out.startswith(f"[{TOOL} failed: ") and expected in out
    assert KEY not in out and KEY not in caplog.text
