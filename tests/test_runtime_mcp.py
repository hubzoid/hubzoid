"""OpenAIAgentsRuntime must connect MCP servers before a run.

Regression: on the OpenAI/Azure backend the loader returns *unconnected*
MCPServerStdio objects; without an explicit connect() the SDK raises
"Server not initialized" on the first tool listing. The Claude backend hides
this (it manages MCP itself), so it only bit when a hub with a connector ran
on Azure. _ensure_mcp() connects once, caches, and drops servers that fail.
"""
from __future__ import annotations

import asyncio
import types

from hubzoid.runtime import OpenAIAgentsRuntime


class _FakeServer:
    def __init__(self, name, *, fail=False):
        self.name = name
        self.fail = fail
        self.connect_calls = 0

    async def connect(self):
        self.connect_calls += 1
        if self.fail:
            raise RuntimeError("boom")


def _runtime(servers):
    agent = types.SimpleNamespace(name="t", mcp_servers=servers)
    return OpenAIAgentsRuntime(agent)


def test_ensure_mcp_connects_servers():
    s = _FakeServer("ok")
    rt = _runtime([s])
    asyncio.run(rt._ensure_mcp())
    assert s.connect_calls == 1
    assert rt._agent.mcp_servers == [s]


def test_ensure_mcp_is_cached():
    s = _FakeServer("ok")
    rt = _runtime([s])

    async def go():
        await rt._ensure_mcp()
        await rt._ensure_mcp()

    asyncio.run(go())
    assert s.connect_calls == 1          # connected once, reused


def test_failed_server_is_dropped_not_fatal():
    good = _FakeServer("good")
    bad = _FakeServer("bad", fail=True)
    rt = _runtime([good, bad])
    asyncio.run(rt._ensure_mcp())        # must not raise
    assert rt._agent.mcp_servers == [good]   # dead server removed


def test_no_servers_is_noop():
    rt = _runtime([])
    asyncio.run(rt._ensure_mcp())
    assert rt._mcp_connected
