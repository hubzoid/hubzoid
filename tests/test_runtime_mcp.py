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

    async def __aenter__(self):
        if self.fail:
            raise RuntimeError("boom")
        self.entered = True
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

    asyncio.run(go())
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
