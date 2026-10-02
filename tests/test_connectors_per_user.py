"""The per-turn personal servers in the default UI mode, and the runtimes'
single entry point to them (``owui_mcp.per_user_servers`` dispatches here).

Real MCP servers on loopback answer only the bearers they know and say whom
they think they serve. Tokens are stored directly (the OAuth flow has its own
suite), so these tests are about who gets which server, with which token, and
which tools.
"""
from __future__ import annotations

import json
import time

import pytest
from agents import Agent, FunctionTool, RunContextWrapper, function_tool

from hubzoid import owui_mcp
from hubzoid.access import Identity, identity_scope, store_for
from hubzoid.connectors import per_user, registry, tokens
from tests import connectors_fakes as f

X, Y = "x@example.org", "y@example.org"


@pytest.fixture(scope="module")
def servers():
    bearers = {"tok-x": X, "tok-y": Y}
    with f.bearer_mcp("mail", bearers, {"whoami": lambda who: who or "nobody",
                                        "mail_search": lambda who: f"mail of {who}"}) as mail, \
            f.bearer_mcp("cal", bearers, {"cal_today": lambda who: f"calendar of {who}"}) as cal, \
            f.bearer_mcp("docs", {}, {"whoami": lambda who: who or "nobody"}) as docs:
        yield {"mail": mail, "cal": cal, "docs": docs}


@pytest.fixture
def hub(tmp_path, monkeypatch, servers):
    f.clean_env(monkeypatch)
    hub = f.make_hub(tmp_path, "sales")
    f.accounts(monkeypatch, hub, {X: ("u-x", "user"), Y: ("u-y", "user")})
    registry.create(hub, {"name": "Mail", "url": servers["mail"]}, actor="test")
    registry.create(hub, {"name": "Cal", "url": servers["cal"]}, actor="test")
    registry.create(hub, {"name": "Docs", "url": servers["docs"], "auth_type": "none"},
                    actor="test")
    for cid in ("mail", "cal", "docs"):
        f.grant_connector(hub, cid, X, Y)
    connect(hub, "u-x", X, "mail", "tok-x", servers["mail"])
    connect(hub, "u-y", Y, "mail", "tok-y", servers["mail"])
    connect(hub, "u-y", Y, "cal", "tok-y", servers["cal"])
    return hub


def connect(hub, uid, email, cid, access, url):
    tokens.store(hub, user_id=uid, email=email, connector_id=cid,
                 token={"v": 1, "kind": "oauth", "access_token": access, "refresh_token": None,
                        "expires_at": time.time() + 3600, "url": url})


def who(email, surface="web"):
    return Identity.make(user=email, groups=[], surface=surface)


def keys(servers_):
    return [s.key for s in servers_]


# ---------------------------------------------------------------------------
# The descriptor
# ---------------------------------------------------------------------------
def test_each_caller_gets_their_own_servers_and_token(hub, servers):
    (mail,) = per_user.per_user_servers(hub, who(X))
    assert isinstance(mail, owui_mcp.PerUserServer)
    assert (mail.key, mail.url, mail.server_id, mail.app) == ("my_mail", servers["mail"], "mail", "mail")
    assert mail.headers == {"Authorization": "Bearer tok-x"} and mail.allowed_tools is None
    assert "tok-x" not in repr(mail)
    ys = per_user.per_user_servers(hub, who(Y))
    assert keys(ys) == ["my_cal", "my_mail"]
    assert {s.headers["Authorization"] for s in ys} == {"Bearer tok-y"}


def test_the_current_identity_is_the_default(hub):
    with identity_scope(who(X)):
        assert keys(per_user.per_user_servers(hub)) == ["my_mail"]


def test_owui_mcp_dispatches_here_in_the_default_mode(hub, servers):
    assert owui_mcp.per_user_servers(hub, who(X)) == per_user.per_user_servers(hub, who(X))
    specs, allowed = owui_mcp.per_user_specs(hub, who(X))
    assert specs == {"my_mail": {"type": "http", "url": servers["mail"],
                                 "headers": {"Authorization": "Bearer tok-x"}}}
    assert allowed == ["mcp__my_mail__*"]


def test_open_webui_mode_still_reads_open_webui_and_only_it(hub, tmp_path, monkeypatch, servers):
    from tests import connect_helpers as h

    db = tmp_path / "webui.db"
    h.seed_owui(db, users=[("ox", X)], secret="legacy",
                servers=[{"id": "odoo", "name": "Odoo", "url": servers["mail"]}])
    h.connect(db, user_id="ox", server_id="odoo", secret="legacy", access_token="tok-x")
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    monkeypatch.setenv("WEBUI_SECRET_KEY", "legacy")
    monkeypatch.setenv("OWUI_NATIVE_MCP", "true")
    f.grant_connector(hub, "odoo", X)
    # Default mode: Open WebUI's connections are not read at all.
    assert keys(owui_mcp.per_user_servers(hub, who(X))) == ["my_mail"]
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    owui = owui_mcp.per_user_servers(hub, who(X, "owui"))
    assert keys(owui) == ["owui_odoo"] and owui[0].headers == {"Authorization": "Bearer tok-x"}


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("surface", ["slack", "slack-channel", "telegram", "whatsapp", "system"])
def test_surfaces_that_cannot_carry_personal_tokens_get_nothing(hub, surface):
    assert per_user.per_user_servers(hub, who(X, surface)) == []


def test_whatsapp_listed_as_a_restricted_surface_gets_them(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_RESTRICTED_SURFACES", "web,whatsapp")
    assert keys(per_user.per_user_servers(hub, who(X, "whatsapp"))) == ["my_mail"]
    assert keys(per_user.per_user_servers(hub, who(X, "workflow"))) == []


def test_workflows_act_as_their_person(hub):
    assert keys(per_user.per_user_servers(hub, who(Y, "workflow"))) == ["my_cal", "my_mail"]


def test_nobody_unknown_or_blocked_gets_nothing(hub):
    assert per_user.per_user_servers(hub, Identity.make(user=None, surface="web")) == []
    assert per_user.per_user_servers(hub, who("ghost@example.org")) == []
    store_for(hub).suspend(X, actor="test")
    assert per_user.per_user_servers(hub, who(X)) == []


def test_switched_off_expired_and_moved_connections_are_skipped(hub, servers):
    registry.update(hub, "cal", {"enabled": False}, actor="test")
    assert keys(per_user.per_user_servers(hub, who(Y))) == ["my_mail"]
    tokens._finish(hub, "u-y", "mail", tokens.get(hub, "u-y", "mail").version,  # noqa: SLF001
                   status="expired", error="test")
    assert per_user.per_user_servers(hub, who(Y)) == []
    # A token is never sent to a server other than the one that issued it.
    from sqlalchemy import text

    from hubzoid import connectors

    with connectors.engine(hub).begin() as conn:
        conn.execute(text("UPDATE hz_connectors SET url = :u WHERE id = 'mail'"),
                     {"u": servers["cal"]})
    assert per_user.per_user_servers(hub, who(X)) == []


def test_each_server_needs_the_connector_capability(hub):
    gs = store_for(hub)
    for cid in ("mail", "cal", "docs"):
        gs.revoke(Y, "sales", "connector_" + cid, actor="test")
    assert per_user.per_user_servers(hub, who(Y)) == []
    gs.grant(Y, "sales", "connector_cal", actor="test")
    assert keys(per_user.per_user_servers(hub, who(Y))) == ["my_cal"]
    gs.grant(Y, "sales", "connector_mail", actor="test")
    assert keys(per_user.per_user_servers(hub, who(Y))) == ["my_cal", "my_mail"]
    gs.revoke(Y, "sales", "connector_cal", actor="test")
    assert keys(per_user.per_user_servers(hub, who(Y))) == ["my_mail"]


def test_a_personal_server_never_replaces_a_hub_server(hub):
    assert per_user.per_user_servers(hub, who(X), reserved={"my_mail"}) == []
    specs, _ = owui_mcp.per_user_specs(hub, who(Y), reserved={"hubzoid", "my_cal"})
    assert set(specs) == {"my_mail"}
    # The hub's own MCP servers are reserved by default.
    (hub / "connectors").mkdir()
    (hub / "connectors" / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"my_mail": {"type": "http", "url": "https://hub-owned.example/mcp"}}}))
    assert keys(per_user.per_user_servers(hub, who(Y))) == ["my_cal"]


def test_the_allow_list_limits_the_tools(hub):
    registry.update(hub, "mail", {"tool_allowlist": ["whoami"]}, actor="test")
    (mail,) = per_user.per_user_servers(hub, who(X))
    assert mail.allowed_tools == ("whoami",)
    _, allowed = owui_mcp.per_user_specs(hub, who(X))
    assert allowed == ["mcp__my_mail__whoami"]


# ---------------------------------------------------------------------------
# Every runtime reaches the server as the caller
# ---------------------------------------------------------------------------
async def _call(tool: FunctionTool) -> str:
    from agents import RunConfig
    from agents.tool_context import ToolContext

    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="c1", tool_arguments="{}",
                      run_config=RunConfig())
    out = await tool.on_invoke_tool(ctx, "{}")
    text_ = out if isinstance(out, str) else json.dumps(out)
    try:
        data = json.loads(text_)
    except ValueError:
        return text_
    if isinstance(data, dict):
        return data.get("text", text_)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0].get("text", text_)
    return text_


@function_tool
def hub_note() -> str:
    """A hub tool."""
    return "hub"


@pytest.mark.asyncio
async def test_claude_turns_carry_the_callers_servers(hub):
    from claude_agent_sdk import ClaudeAgentOptions
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from hubzoid.factory_claude import ClaudeRuntime

    base = ClaudeAgentOptions(mcp_servers={"hubzoid": {"type": "sdk"}},
                              allowed_tools=["mcp__hubzoid__hub_note"])
    rt = ClaudeRuntime(name="t", options=base, hub_dir=hub)
    with identity_scope(who(X)):
        opts = rt._options_for_turn()
    specs = {k: v for k, v in opts.mcp_servers.items() if k != "hubzoid"}
    assert set(specs) == {"my_mail"} and "mcp__my_mail__*" in opts.allowed_tools
    assert base.mcp_servers == {"hubzoid": {"type": "sdk"}}  # shared options untouched
    async with streamablehttp_client(specs["my_mail"]["url"],
                                     headers=specs["my_mail"]["headers"]) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool("whoami", {})
    assert res.content[0].text == X


@pytest.mark.asyncio
async def test_openai_turns_run_on_a_clone_with_the_callers_servers(hub, monkeypatch):
    import agents

    from hubzoid.runtime import OpenAIAgentsRuntime

    seen = {}

    def probe(agent, run_input, max_turns=None):  # noqa: ARG001
        class Result:
            async def stream_events(self):
                tools = await agent.get_mcp_tools(RunContextWrapper(context=None))
                seen["servers"] = [s.name for s in agent.mcp_servers]
                seen["tools"] = sorted(t.name for t in tools)
                seen["whoami"] = await _call(next(t for t in tools if t.name == "whoami"))
                if False:  # pragma: no cover - makes this an async generator
                    yield None
        return Result()

    monkeypatch.setattr(agents.Runner, "run_streamed", probe)
    registry.update(hub, "mail", {"tool_allowlist": ["whoami"]}, actor="test")
    agent = Agent(name="t", instructions="", tools=[hub_note], mcp_servers=[])
    rt = OpenAIAgentsRuntime(agent, hub_dir=hub, vision=(False, 0, 0))
    with identity_scope(who(X)):
        await rt.run("hi")
    assert seen == {"servers": ["my_mail"], "tools": ["whoami"], "whoami": X}
    assert rt._agent.mcp_servers == []


@pytest.mark.asyncio
async def test_codex_turns_use_a_per_turn_registry_with_the_callers_tools(hub, monkeypatch):
    from hubzoid.factory_codex import CodexRuntime

    seen = []

    async def fake_stream(self, prompt, registry_):  # noqa: ARG001
        seen.append({"names": sorted(registry_),
                     "whoami": await _call(registry_["whoami"]) if "whoami" in registry_ else None})
        yield "ok"

    monkeypatch.setattr(CodexRuntime, "_stream", fake_stream)
    rt = CodexRuntime(name="t", instructions="", registry={"hub_note": hub_note}, hub_dir=hub,
                      personal_mcp=True, tool_mode="off")
    with identity_scope(who(Y)):
        assert await rt.run("hi") == "ok"
    assert seen[-1] == {"names": ["cal_today", "hub_note", "mail_search", "whoami"], "whoami": Y}
    assert list(rt.registry) == ["hub_note"]


@pytest.mark.asyncio
async def test_a_connector_without_sign_in_carries_no_credentials(hub):
    from contextlib import AsyncExitStack

    from hubzoid.runtime import open_personal_mcp

    tokens.store(hub, user_id="u-x", email=X, connector_id="docs",
                 token={"v": 1, "kind": "none", "url": registry.get(hub, "docs").url})
    found = per_user.per_user_servers(hub, who(X))
    assert keys(found) == ["my_docs", "my_mail"]
    docs = found[0]
    assert docs.headers == {} and docs.server_id == "docs"
    async with AsyncExitStack() as stack:
        ((_server, tools),) = await open_personal_mcp(stack, [docs], set())
        assert await _call(next(t for t in tools if t.name == "whoami")) == "nobody"
    # Opting out is a disconnect like any other.
    tokens.disconnect(hub, "u-x", "docs")
    assert keys(per_user.per_user_servers(hub, who(X))) == ["my_mail"]
