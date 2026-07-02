"""End-to-end: a claude-local hub delegates to a different-tier subagent.

Proves the whole delegation path with a real `claude` CLI: the main agent
(sonnet) dispatches the `opus-helper` subagent (opus tier) via the Agent
spawn tool, the subagent runs and returns, and the main agent relays it.

Self-skips when the `claude` CLI is absent or claude-agent-sdk is missing.
Run: cd HubZoid && pytest tests/e2e/test_delegate_e2e.py -m e2e -v
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

HUB = Path(__file__).resolve().parent.parent / "fixtures" / "delegate_claude_hub"


@pytest.fixture(autouse=True)
def _require_claude_local():
    if shutil.which("claude") is None:
        pytest.skip("`claude` CLI not on PATH — claude-local e2e needs it")
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        pytest.skip("claude_agent_sdk not installed")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MODEL", "claude-local")
    monkeypatch.setenv("BRIDGE_API_KEYS", "e2e-dev")
    yield


def test_delegate_dispatches_and_returns():
    from hubzoid.factory_claude import build_claude_runtime

    rt = build_claude_runtime(HUB)

    async def collect() -> str:
        chunks: list[str] = []
        async for c in rt.stream(
            "Delegate this hard question to opus-helper: In one word, what is "
            "the opposite of 'up'? Relay its answer."
        ):
            chunks.append(c)
        return "".join(chunks)

    out = asyncio.new_event_loop().run_until_complete(collect())
    # The subagent dispatch surfaces as a tool-activity marker for the Agent tool.
    assert "✓" in out, f"no tool-activity marker (no dispatch?):\n{out!r}"
    # And the answer makes it back through the main agent.
    assert "down" in out.lower(), f"delegate answer not relayed:\n{out!r}"
