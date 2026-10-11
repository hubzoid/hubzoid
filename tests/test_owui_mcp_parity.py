"""Personal (per-user) MCP servers reach every runtime the same way.

A real in-process MCP HTTP server answers only the bearers it knows and tells
the caller who it thinks they are. For person X and person Y on one hub, the
per-turn tool surface of Claude (options), OpenAI Agents (the cloned agent) and
Codex (the per-turn registry) carries only that person's servers with that
person's token. The shared agent, options and registry never change, a Slack
channel gets nothing, and a personal tool never shadows a hub tool.

No model is called: each runtime's model step is replaced by a probe that uses
the tools it was given.
"""
from __future__ import annotations

import json

import pytest
from agents import Agent, FunctionTool, RunContextWrapper, function_tool

from hubzoid import owui_mcp
from hubzoid.access import Identity, identity_scope
from tests import connect_helpers as h

X, Y = "x@example.org", "y@example.org"
SECRET = "parity-secret"


@pytest.fixture(scope="module")
def servers():
    bearers = {"tok-x": X, "tok-y": Y}
    with h.mcp_server("mail", bearers, {"whoami": lambda who: who or "nobody",
                                        "mail_search": lambda who: f"mail of {who}"}) as mail, \
            h.mcp_server("cal", bearers, {"cal_today": lambda who: f"calendar of {who}"}) as cal:
        yield {"mail": mail, "cal": cal}


@pytest.fixture
def hub(tmp_path, monkeypatch, servers):
    hub = tmp_path / "parityhub"
    hub.mkdir()
    db = tmp_path / "webui.db"
    h.seed_owui(db, users=[("ux", X), ("uy", Y)], secret=SECRET, servers=[
        {"id": "mail", "name": "Mail", "url": servers["mail"]},
        {"id": "cal", "name": "Calendar", "url": servers["cal"]},
    ])
    h.connect(db, user_id="ux", server_id="mail", secret=SECRET, access_token="tok-x")
    h.connect(db, user_id="uy", server_id="mail", secret=SECRET, access_token="tok-y")
    h.connect(db, user_id="uy", server_id="cal", secret=SECRET, access_token="tok-y")
    h.owui_env(monkeypatch, db, SECRET)
    h.isolated_store(tmp_path, monkeypatch)
    monkeypatch.delenv("HUBZOID_RESTRICTED_SURFACES", raising=False)
    h.grant(hub, X, Y, permissions=("connector_mail", "connector_cal"))
    return hub


def _who(email, surface="owui"):
    return Identity.make(user=email, groups=[], surface=surface)


@function_tool
def hub_note() -> str:
    """A hub tool."""
    return "hub"


async def _call(tool: FunctionTool, name: str) -> str:
    from agents import RunConfig
    from agents.tool_context import ToolContext

    ctx = ToolContext(context=None, tool_name=name, tool_call_id="c1", tool_arguments="{}",
                      run_config=RunConfig())
    out = await tool.on_invoke_tool(ctx, "{}")
    return out if isinstance(out, str) else json.dumps(out)


def _text(result: str) -> str:
    # MCP tool output may come back as JSON content blocks.
    try:
        data = json.loads(result)
    except ValueError:
        return result
    if isinstance(data, dict) and "text" in data:
        return data["text"]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0].get("text", result)
    return result


# ---------------------------------------------------------------------------
# Claude: per-turn options
# ---------------------------------------------------------------------------
async def _claude_surface(hub, ident):
    from claude_agent_sdk import ClaudeAgentOptions

    from hubzoid.factory_claude import ClaudeRuntime

    base = ClaudeAgentOptions(mcp_servers={"hubzoid": {"type": "sdk"}},
                              allowed_tools=["mcp__hubzoid__hub_note"])
    rt = ClaudeRuntime(name="t", options=base, hub_dir=hub)
    with identity_scope(ident):
        opts = rt._options_for_turn()
    assert base.mcp_servers == {"hubzoid": {"type": "sdk"}}  # shared options untouched
    return {k: v for k, v in opts.mcp_servers.items() if k != "hubzoid"}, opts.allowed_tools


async def _whoami_over_http(spec: dict, tool: str = "whoami") -> str:
    """Use a Claude spec the way the Claude CLI would: an MCP HTTP client with
    the spec's headers."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(spec["url"], headers=spec["headers"]) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(tool, {})
            return res.content[0].text


@pytest.mark.asyncio
async def test_claude_turn_carries_only_the_callers_servers(hub):
    specs, allowed = await _claude_surface(hub, _who(X))
    assert set(specs) == {"my_mail"}
    assert specs["my_mail"]["headers"] == {"Authorization": "Bearer tok-x"}
    assert "mcp__my_mail__*" in allowed
    assert await _whoami_over_http(specs["my_mail"]) == X

    specs, _ = await _claude_surface(hub, _who(Y))
    assert set(specs) == {"my_mail", "my_cal"}
    assert await _whoami_over_http(specs["my_mail"]) == Y


# ---------------------------------------------------------------------------
# OpenAI Agents: the cloned agent
# ---------------------------------------------------------------------------
def _connector_tools(agent):
    """The connector tools a turn's agent carries: namespaced function tools."""
    return [t for t in agent.tools if t.name.startswith("mcp__")]


def _servers(agent):
    return sorted({t.name.split("__")[1] for t in _connector_tools(agent)})


class _Probe:
    """Stands in for Runner.run_streamed: records the agent it was given and
    calls its connector tools while the servers are connected."""

    def __init__(self):
        self.agents = []
        self.calls = {}

    def __call__(self, agent, run_input, max_turns=None):  # noqa: ARG002
        probe = self

        class Result:
            async def stream_events(self):
                probe.agents.append(agent)
                tools = _connector_tools(agent)
                for t in tools:
                    short = t.name.split("__")[-1]
                    if short in ("whoami", "cal_today"):
                        probe.calls[short] = _text(await _call(t, t.name))
                probe.names = sorted(t.name.split("__")[-1] for t in tools)
                if False:  # pragma: no cover - makes this an async generator
                    yield None

        return Result()


def _openai_runtime(hub, tools=(hub_note,)):
    from hubzoid.runtime import OpenAIAgentsRuntime

    agent = Agent(name="t", instructions="", tools=list(tools), mcp_servers=[])
    return OpenAIAgentsRuntime(agent, hub_dir=hub, vision=(False, 0, 0))


@pytest.mark.asyncio
async def test_openai_turn_runs_on_a_clone_with_only_the_callers_servers(hub, monkeypatch):
    import agents

    probe = _Probe()
    monkeypatch.setattr(agents.Runner, "run_streamed", probe)
    rt = _openai_runtime(hub)
    with identity_scope(_who(X)):
        await rt.run("hi")
    (cloned,) = probe.agents
    assert cloned is not rt._agent
    assert _servers(cloned) == ["my_mail"]
    assert sorted(t.name for t in _connector_tools(cloned)) == [
        "mcp__my_mail__mail_search", "mcp__my_mail__whoami"]  # the names Claude uses too
    assert probe.names == ["mail_search", "whoami"]
    assert probe.calls == {"whoami": X}
    assert [t.name for t in rt._agent.tools] == ["hub_note"]  # the shared agent is unchanged

    probe2 = _Probe()
    monkeypatch.setattr(agents.Runner, "run_streamed", probe2)
    with identity_scope(_who(Y)):
        await rt.run("hi")
    assert _servers(probe2.agents[0]) == ["my_cal", "my_mail"]
    assert probe2.calls == {"whoami": Y, "cal_today": f"calendar of {Y}"}


# ---------------------------------------------------------------------------
# Codex: the per-turn registry
# ---------------------------------------------------------------------------
def _codex_runtime(hub, registry=None):
    from hubzoid.factory_codex import CodexRuntime

    return CodexRuntime(name="t", instructions="", registry=dict(registry or {"hub_note": hub_note}),
                        hub_dir=hub, personal_mcp=True, tool_mode="off")


def _probe_codex(monkeypatch, seen: list):
    from hubzoid.factory_codex import CodexRuntime

    async def fake_stream(self, prompt, registry):  # noqa: ARG001
        entry = {"names": sorted(registry), "shared": registry is self.registry}
        who = "mcp__my_mail__whoami"
        if who in registry:
            entry["whoami"] = _text(await _call(registry[who], who))
        seen.append(entry)
        yield "ok"

    monkeypatch.setattr(CodexRuntime, "_stream", fake_stream)


@pytest.mark.asyncio
async def test_codex_turn_uses_a_per_turn_registry_with_only_the_callers_tools(hub, monkeypatch):
    seen: list = []
    _probe_codex(monkeypatch, seen)
    rt = _codex_runtime(hub)
    with identity_scope(_who(X)):
        assert await rt.run("hi") == "ok"
    assert seen[-1] == {"names": ["hub_note", "mcp__my_mail__mail_search", "mcp__my_mail__whoami"], "shared": False,
                        "whoami": X}
    assert list(rt.registry) == ["hub_note"]  # the shared registry is unchanged
    assert rt.last_error is None

    with identity_scope(_who(Y)):
        await rt.run("hi")
    assert seen[-1]["names"] == ["hub_note", "mcp__my_cal__cal_today", "mcp__my_mail__mail_search",
                                 "mcp__my_mail__whoami"]
    assert seen[-1]["whoami"] == Y
    assert list(rt.registry) == ["hub_note"]


@pytest.mark.asyncio
async def test_codex_delegates_never_carry_personal_tools(hub, monkeypatch):
    from hubzoid.factory_codex import CodexRuntime

    seen: list = []
    _probe_codex(monkeypatch, seen)
    child = CodexRuntime(name="d", instructions="", registry={"hub_note": hub_note},
                         hub_dir=hub, tool_mode="off")
    with identity_scope(_who(X)):
        await child.run("hi")
    assert seen[-1] == {"names": ["hub_note"], "shared": True}


# ---------------------------------------------------------------------------
# Refusals, identical across the three
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["slack-channel", "slack", "telegram", "whatsapp"])
async def test_untrusted_surfaces_get_no_personal_servers_on_any_runtime(hub, monkeypatch, surface):
    import agents

    specs, _ = await _claude_surface(hub, _who(X, surface))
    assert specs == {}

    probe = _Probe()
    monkeypatch.setattr(agents.Runner, "run_streamed", probe)
    rt = _openai_runtime(hub)
    with identity_scope(_who(X, surface)):
        await rt.run("hi")
    assert probe.agents[0] is rt._agent

    seen: list = []
    _probe_codex(monkeypatch, seen)
    with identity_scope(_who(X, surface)):
        await _codex_runtime(hub).run("hi")
    assert seen[-1]["shared"] is True


@pytest.mark.asyncio
async def test_whatsapp_listed_as_restricted_surface_reaches_all_three(hub, monkeypatch):
    import agents

    monkeypatch.setenv("HUBZOID_RESTRICTED_SURFACES", "owui,whatsapp")
    ident = _who(X, "whatsapp")
    specs, _ = await _claude_surface(hub, ident)
    assert set(specs) == {"my_mail"}

    probe = _Probe()
    monkeypatch.setattr(agents.Runner, "run_streamed", probe)
    with identity_scope(ident):
        await _openai_runtime(hub).run("hi")
    assert probe.calls == {"whoami": X}

    seen: list = []
    _probe_codex(monkeypatch, seen)
    with identity_scope(ident):
        await _codex_runtime(hub).run("hi")
    assert seen[-1]["whoami"] == X


@pytest.mark.asyncio
async def test_each_runtime_needs_the_connector_grant(hub, monkeypatch):
    import agents

    import hubzoid.access as access

    gs = access.store_for(hub)
    gs.revoke(X, hub.name, owui_mcp.capability("mail"), actor="test")
    gs.grant(X, hub.name, "use_hub")
    specs, _ = await _claude_surface(hub, _who(X))
    assert specs == {}
    gs.grant(X, hub.name, owui_mcp.capability("mail"))
    specs, _ = await _claude_surface(hub, _who(X))
    assert set(specs) == {"my_mail"}

    probe = _Probe()
    monkeypatch.setattr(agents.Runner, "run_streamed", probe)
    with identity_scope(_who(X)):
        await _openai_runtime(hub).run("hi")
    assert probe.calls == {"whoami": X}

    seen: list = []
    _probe_codex(monkeypatch, seen)
    with identity_scope(_who(X)):
        await _codex_runtime(hub).run("hi")
    assert seen[-1]["whoami"] == X


@pytest.mark.asyncio
async def test_a_connector_tool_never_shadows_a_hub_tool(hub, monkeypatch):
    """Every runtime names connector tools mcp__<server>__<tool>, as Claude
    does, so a connector tool named like a hub tool sits beside it instead of
    replacing it or being dropped."""
    import agents

    @function_tool
    def whoami() -> str:
        """The hub's own whoami."""
        return "hub"

    probe = _Probe()
    monkeypatch.setattr(agents.Runner, "run_streamed", probe)
    rt = _openai_runtime(hub, tools=(hub_note, whoami))
    with identity_scope(_who(X)):
        await rt.run("hi")
    names = [t.name for t in probe.agents[0].tools]
    assert "whoami" in names and "mcp__my_mail__whoami" in names
    assert probe.calls == {"whoami": X}  # the connector's, as X

    seen: list = []
    _probe_codex(monkeypatch, seen)
    with identity_scope(_who(X)):
        await _codex_runtime(hub, {"hub_note": hub_note, "whoami": whoami}).run("hi")
    assert seen[-1]["names"] == ["hub_note", "mcp__my_mail__mail_search", "mcp__my_mail__whoami",
                                 "whoami"]


@pytest.mark.asyncio
async def test_a_dead_personal_server_never_breaks_the_turn(hub, monkeypatch):
    import agents

    import dataclasses

    from hubzoid.connectors import registry

    real = registry.list_all

    def moved(hub_dir):  # nothing listens here
        return [dataclasses.replace(c, url="http://127.0.0.1:9/mcp") for c in real(hub_dir)]

    monkeypatch.setattr(registry, "list_all", moved)
    probe = _Probe()
    monkeypatch.setattr(agents.Runner, "run_streamed", probe)
    rt = _openai_runtime(hub)
    with identity_scope(_who(X)):
        await rt.run("hi")
    assert _servers(probe.agents[0]) == []
    assert rt.last_error is None


def test_parity_spec_is_the_same_data_for_every_runtime(hub):
    """One neutral source: the Claude spec and the OpenAI/Codex client are
    built from the same PerUserServer."""
    (srv_,) = owui_mcp.per_user_servers(hub, _who(X))
    specs, _ = owui_mcp.per_user_specs(hub, _who(X))
    assert specs[srv_.key]["url"] == srv_.url
    assert specs[srv_.key]["headers"] == srv_.headers

    from hubzoid.runtime import personal_mcp_server
    client = personal_mcp_server(srv_)
    assert client.name == srv_.key


def test_connector_tool_names_are_valid_and_unique_on_every_runtime():
    """mcp__<server>__<tool>, as Claude names them: within the 64 characters
    providers accept, and two long names never collapse into one."""
    from hubzoid.runtime import connector_tool_name

    import re

    assert connector_tool_name("my_mail", "search") == "mcp__my_mail__search"
    dotted, slashed = connector_tool_name("my_jira", "issue.get"), connector_tool_name("my_jira", "issue/get")
    assert dotted != slashed and dotted.startswith("mcp__my_jira__issue_get_")
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", n) for n in (dotted, slashed))
    assert connector_tool_name("my_jira", "issue_get") == "mcp__my_jira__issue_get"
    a = connector_tool_name("my_mail", "x" * 80 + "a")
    b = connector_tool_name("my_mail", "x" * 80 + "b")
    assert len(a) == len(b) == 64 and a != b and a.startswith("mcp__my_mail__")
