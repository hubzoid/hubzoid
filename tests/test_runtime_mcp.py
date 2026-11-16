"""OpenAIAgentsRuntime MCP lifecycle: aopen() connects, aclose() tears down.

Regression history:
  1. On the OpenAI/Azure backend the loader returns *unconnected* MCP servers;
     without connecting, the SDK raises "Server not initialized".
  2. Connecting lazily inside a per-request stream() then bound the stdio
     server's cancel scope to a transient task, giving "cancel scope in a
     different task" / ClosedResourceError on teardown or the next request.

The fix: aopen()/aclose() connect and disconnect in one stable task (the
bridge lifespan, or a one-shot CLI/schedule coroutine). A server that fails to
connect is dropped, not fatal. The Claude backend manages MCP itself, so its
aopen/aclose are no-ops.
"""
from __future__ import annotations

import asyncio
import types

from hubzoid.runtime import OpenAIAgentsRuntime


class _FakeServer:
    """Async-context-manager stand-in for an Agents-SDK MCP server."""

    def __init__(self, name, *, fail=False):
        self.name = name
        self.fail = fail
        self.entered = False
        self.exited = False
        self.enters = 0

    async def __aenter__(self):
        if self.fail:
            raise RuntimeError("boom")
        self.entered = True
        self.enters += 1
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False


def _runtime(servers):
    agent = types.SimpleNamespace(name="t", mcp_servers=servers)
    return OpenAIAgentsRuntime(agent)


def test_aopen_connects_then_aclose_disconnects():
    s = _FakeServer("ok")
    rt = _runtime([s])

    async def go():
        await rt.aopen()
        assert s.entered
        assert rt._agent.mcp_servers == [s]
        await rt.aclose()
        assert s.exited

    asyncio.run(go())


def test_aopen_is_idempotent():
    s = _FakeServer("ok")
    rt = _runtime([s])

    async def go():
        await rt.aopen()
        await rt.aopen()          # second call is a no-op, doesn't re-enter
        await rt.aclose()

    asyncio.run(go())
    assert s.enters == 1
    assert s.entered


def test_failed_server_is_dropped_not_fatal():
    good = _FakeServer("good")
    bad = _FakeServer("bad", fail=True)
    rt = _runtime([good, bad])

    async def go():
        await rt.aopen()          # must not raise
        await rt.aclose()

    asyncio.run(go())
    assert rt._agent.mcp_servers == [good]   # dead server removed


def test_no_servers_is_noop():
    rt = _runtime([])

    async def go():
        await rt.aopen()
        await rt.aclose()

    asyncio.run(go())
    assert rt._agent.mcp_servers == []


def test_a_server_whose_variable_is_unset_stays_off(tmp_path, monkeypatch):
    """`Bearer ${GITHUB_TOKEN}` with no token would fail on every turn with a
    misleading error; the server stays off until the variable is set."""
    import json

    from hubzoid.loaders import mcp as mcp_loader

    (tmp_path / "connectors").mkdir()
    (tmp_path / "connectors" / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "github": {"transport": "streamable-http", "url": "https://api.example.test/mcp",
                   "headers": {"Authorization": "Bearer ${HZ_TEST_GH_TOKEN}"}},
        "plain": {"command": "echo", "args": ["hi"]}}}))
    monkeypatch.delenv("HZ_TEST_GH_TOKEN", raising=False)
    assert set(mcp_loader.load_all_raw(tmp_path)) == {"plain"}
    monkeypatch.setenv("HZ_TEST_GH_TOKEN", "t0ken")
    servers = mcp_loader.load_all_raw(tmp_path)
    assert servers["github"]["headers"]["Authorization"] == "Bearer t0ken"


def test_an_empty_env_value_is_passed_not_a_reason_to_turn_off(tmp_path, monkeypatch):
    import json

    from hubzoid.loaders import mcp as mcp_loader

    (tmp_path / "connectors").mkdir()
    (tmp_path / "connectors" / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "tool": {"command": "echo", "env": {"HTTP_PROXY": "${HZ_TEST_PROXY}"}}}}))
    monkeypatch.setenv("HZ_TEST_PROXY", "")
    assert "tool" in mcp_loader.load_all_raw(tmp_path)
