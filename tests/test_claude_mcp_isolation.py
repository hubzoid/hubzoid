"""Every claude-local run is MCP-isolated: the `claude` subprocess gets
`--strict-mcp-config` and only the servers Hubzoid passes, so the host
account's claude.ai connectors and user/project MCP settings never reach a
chat user. Checked on the exact CLI command the SDK builds, for chat turns
(with per-user connectors and gated tools), `hub.call_llm` and the eval judge.
An SDK that cannot isolate is refused. No model calls; the live check is
tests/e2e/test_claude_mcp_isolation_e2e.py.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sys

import pytest

from hubzoid import factory_claude, owui_mcp
from hubzoid.access import Identity, identity_scope, store_for


def _cli(options) -> list[str]:
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    # A fixed CLI path: the command is built, never run.
    options = dataclasses.replace(options, cli_path="/usr/local/bin/claude")
    return SubprocessCLITransport(prompt="x", options=options)._build_command()


def _mcp_config(cmd: list[str]) -> dict:
    return json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"] if "--mcp-config" in cmd else {}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    d = tmp_path / "support"
    (d / "connectors").mkdir(parents=True)
    (d / "AGENTS.md").write_text("---\nname: support\ndescription: d\n---\nHelp.\n")
    (d / "connectors" / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "hub-echo": {"command": sys.executable, "args": ["-c", "print('synthetic')"]}}}))
    for k in ("HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_BROWSER", "HUBZOID_RESTRICTED_SURFACES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    gs = store_for(d)
    gs.set_authoritative(True, hub="support")
    gs.grant("ana@example.org", "support", "jev", actor="test")
    gs.grant("ben@example.org", "support", "use_hub", actor="test")
    return d


def _per_user(monkeypatch):
    """Stand in for Open WebUI: each person has their own connected server and token."""
    def specs(hub_dir, ident, **_kw):
        who = (ident.user or "").split("@")[0]
        if who not in ("ana", "ben"):
            return {}, []
        key = f"notes-{who}"
        return ({key: {"type": "http", "url": f"http://127.0.0.1:9/{who}/mcp",
                       "headers": {"Authorization": f"Bearer token-of-{who}"}}},
                [f"mcp__{key}__*"])
    monkeypatch.setattr(owui_mcp, "per_user_specs", specs)


# --- chat ----------------------------------------------------------------------
def test_chat_runtime_is_strict_and_passes_only_hub_servers(hub):
    rt = factory_claude.build_claude_runtime(hub)
    assert rt._options.strict_mcp_config is True
    cmd = _cli(rt._options)
    assert "--strict-mcp-config" in cmd
    assert set(_mcp_config(cmd)) == {"hubzoid", "hub-echo"}


@pytest.mark.parametrize("user,surface", [
    ("ana@example.org", "owui"), ("ben@example.org", "owui"), ("ana@example.org", "slack"), (None, "system")])
def test_every_turn_stays_strict(hub, monkeypatch, user, surface):
    _per_user(monkeypatch)
    rt = factory_claude.build_claude_runtime(hub)
    with identity_scope(Identity.make(user, surface=surface)):
        opts = rt._options_for_turn()
    assert opts.strict_mcp_config is True
    assert "--strict-mcp-config" in _cli(opts)


def test_per_user_connectors_are_injected_and_isolated(hub, monkeypatch):
    _per_user(monkeypatch)
    rt = factory_claude.build_claude_runtime(hub)
    base_servers, base_allowed = dict(rt._options.mcp_servers), list(rt._options.allowed_tools)
    cmds = {}
    for who in ("ana", "ben"):
        with identity_scope(Identity.make(f"{who}@example.org", surface="owui")):
            opts = rt._options_for_turn()
        cmds[who] = _cli(opts)
        servers = _mcp_config(cmds[who])
        assert set(servers) == {"hubzoid", "hub-echo", f"notes-{who}"}
        assert servers[f"notes-{who}"]["headers"]["Authorization"] == f"Bearer token-of-{who}"
        assert f"mcp__notes-{who}__*" in opts.allowed_tools
    assert "token-of-ben" not in " ".join(cmds["ana"]) and "token-of-ana" not in " ".join(cmds["ben"])
    # call_jev is granted to ana only; the per-user injection does not undo the gate
    assert "mcp__hubzoid__call_jev" in " ".join(cmds["ana"])
    assert "mcp__hubzoid__call_jev" not in " ".join(cmds["ben"])
    # the shared base options are never mutated by a turn
    assert rt._options.mcp_servers == base_servers and rt._options.allowed_tools == base_allowed


def test_a_shared_channel_gets_no_personal_connector(hub, monkeypatch):
    """The real per_user_specs keeps personal tokens off surfaces that cannot
    reach controlled tools (Slack, WhatsApp): unchanged by strict isolation."""
    monkeypatch.setenv("OWUI_NATIVE_MCP", "true")
    rt = factory_claude.build_claude_runtime(hub)
    with identity_scope(Identity.make("ana@example.org", surface="slack")):
        opts = rt._options_for_turn()
    assert set(_mcp_config(_cli(opts))) == {"hubzoid", "hub-echo"}


# --- call_llm and the eval judge: no tools at all ----------------------------------
def _capture_query(monkeypatch):
    import claude_agent_sdk
    from claude_agent_sdk import AssistantMessage, ResultMessage
    from claude_agent_sdk.types import TextBlock

    seen = {}

    async def fake_query(*, prompt, options):
        seen["options"] = options
        yield AssistantMessage(content=[TextBlock(text='{"ok": true}')], model="claude-haiku-4-5")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id="s", usage={"input_tokens": 5, "output_tokens": 2})

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return seen


def _assert_tool_free(options):
    assert options.strict_mcp_config is True
    assert (options.tools, options.allowed_tools, options.mcp_servers, options.setting_sources,
            options.max_turns) == ([], [], {}, [], 1)
    cmd = _cli(options)
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert "--mcp-config" not in cmd and "--allowedTools" not in cmd


def test_call_llm_is_tool_free(monkeypatch):
    seen = _capture_query(monkeypatch)
    text, _ = asyncio.run(factory_claude.claude_complete("route it", model_setting="claude-local/haiku"))
    assert text == '{"ok": true}'
    _assert_tool_free(seen["options"])
    assert seen["options"].model == "haiku"


def test_eval_judge_is_tool_free(monkeypatch):
    from hubzoid.evals import judge

    seen = _capture_query(monkeypatch)
    asyncio.run(judge._ask_claude_local("claude-local/haiku", "Did the reply pass?"))
    _assert_tool_free(seen["options"])


# --- an SDK without isolation is refused ---------------------------------------------
@pytest.fixture
def sdk_without_isolation(monkeypatch):
    import claude_agent_sdk

    real = claude_agent_sdk.ClaudeAgentOptions
    fields = [(f.name, f.type, dataclasses.field(default=None))
              for f in dataclasses.fields(real) if f.name != "strict_mcp_config"]
    old = dataclasses.make_dataclass("ClaudeAgentOptions", fields)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeAgentOptions", old)


def test_chat_refuses_an_sdk_without_isolation(hub, sdk_without_isolation):
    with pytest.raises(RuntimeError, match="cannot isolate MCP servers"):
        factory_claude.build_claude_runtime(hub)


def test_call_llm_and_judge_refuse_an_sdk_without_isolation(monkeypatch, sdk_without_isolation):
    from hubzoid.evals import judge

    seen = _capture_query(monkeypatch)
    with pytest.raises(RuntimeError, match="cannot isolate MCP servers"):
        asyncio.run(factory_claude.claude_complete("x"))
    with pytest.raises(RuntimeError, match="cannot isolate MCP servers"):
        asyncio.run(judge._ask_claude_local("claude-local", "x"))
    assert "options" not in seen                      # nothing ran
