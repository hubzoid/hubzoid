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
    assert set(specs) == {"owui_mail"}
    assert specs["owui_mail"]["headers"] == {"Authorization": "Bearer tok-x"}
    assert "mcp__owui_mail__*" in allowed
    assert await _whoami_over_http(specs["owui_mail"]) == X

    specs, _ = await _claude_surface(hub, _who(Y))
    assert set(specs) == {"owui_mail", "owui_calendar"}
    assert await _whoami_over_http(specs["owui_mail"]) == Y


# ---------------------------------------------------------------------------
# OpenAI Agents: the cloned agent
# ---------------------------------------------------------------------------
class _Probe:
    """Stands in for Runner.run_streamed: records the agent it was given and
    calls its MCP tools while the servers are connected."""

    def __init__(self):
        self.agents = []
        self.calls = {}

    def __call__(self, agent, run_input, max_turns=None):  # noqa: ARG002
        probe = self

        class Result:
            async def stream_events(self):
                probe.agents.append(agent)
                tools = await agent.get_mcp_tools(RunContextWrapper(context=None))
                for t in tools:
                    if t.name in ("whoami", "cal_today"):
                        probe.calls[t.name] = _text(await _call(t, t.name))
                probe.names = sorted(t.name for t in tools)
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
    assert [s.name for s in cloned.mcp_servers] == ["owui_mail"]
    assert probe.names == ["mail_search", "whoami"]
    assert probe.calls == {"whoami": X}
    assert rt._agent.mcp_servers == []  # the shared agent is unchanged

    probe2 = _Probe()
    monkeypatch.setattr(agents.Runner, "run_streamed", probe2)
    with identity_scope(_who(Y)):
        await rt.run("hi")
    assert sorted(s.name for s in probe2.agents[0].mcp_servers) == ["owui_calendar", "owui_mail"]
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
        if "whoami" in registry:
            entry["whoami"] = _text(await _call(registry["whoami"], "whoami"))
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
    assert seen[-1] == {"names": ["hub_note", "mail_search", "whoami"], "shared": False,
                        "whoami": X}
    assert list(rt.registry) == ["hub_note"]  # the shared registry is unchanged
    assert rt.last_error is None

    with identity_scope(_who(Y)):
        await rt.run("hi")
    assert seen[-1]["names"] == ["cal_today", "hub_note", "mail_search", "whoami"]
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
    assert set(specs) == {"owui_mail"}

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
async def test_managed_hub_needs_the_connector_grant_on_every_runtime(hub, monkeypatch):
    import agents

    import hubzoid.access as access

    gs = access.store_for(hub)
    gs.set_authoritative(True, hub=hub.name)
    gs.grant(X, hub.name, "use_hub")
    specs, _ = await _claude_surface(hub, _who(X))
    assert specs == {}
    gs.grant(X, hub.name, owui_mcp.capability("mail"))
    specs, _ = await _claude_surface(hub, _who(X))
    assert set(specs) == {"owui_mail"}

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
async def test_a_personal_tool_never_shadows_a_hub_tool(hub, monkeypatch):
    """OpenAI and Codex expose MCP tools by their bare name, so a personal
    server with a tool named like a hub tool is skipped for the turn. Claude
    namespaces every server, so its hub tools cannot collide."""
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
    assert probe.agents[0].mcp_servers == []

    seen: list = []
    _probe_codex(monkeypatch, seen)
    with identity_scope(_who(X)):
        await _codex_runtime(hub, {"hub_note": hub_note, "whoami": whoami}).run("hi")
    assert seen[-1]["names"] == ["hub_note", "whoami"]


@pytest.mark.asyncio
async def test_a_dead_personal_server_never_breaks_the_turn(hub, monkeypatch):
    import agents

    from hubzoid.access import owui_tool_servers as srv

    real = srv.list_mcp_connections

    def moved(hub_dir):
        out = real(hub_dir)
        for c in out:
            c["url"] = "http://127.0.0.1:9/mcp"  # nothing listens here
        return out

    monkeypatch.setattr(srv, "list_mcp_connections", moved)
    probe = _Probe()
    monkeypatch.setattr(agents.Runner, "run_streamed", probe)
    rt = _openai_runtime(hub)
    with identity_scope(_who(X)):
        await rt.run("hi")
    assert probe.agents[0].mcp_servers == []
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
