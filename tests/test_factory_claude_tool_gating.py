"""Claude-local backend must disable built-in SDK tools.

The Claude Agent SDK ships with built-in tools (Bash, Read, Edit, Write,
Task, WebFetch, Grep, Glob, etc.) ENABLED by default. `allowed_tools`
in `ClaudeAgentOptions` controls auto-permission, not availability —
the SDK's `tools` field is the actual gate.

Without `tools=[]`, our agents can shell out via Bash and read random
files via Read — including on hallucinated paths under
`~/.claude/projects/...` when they panic over a truncated upload
preview. The system addendum telling the model "don't escape" is
ignored because the tools are right there in its pool.

This is the bug behind the multi-screen rabbit hole in Slack when a
user uploads a 1 MB markdown.
"""
from __future__ import annotations

from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MODEL", "claude-local")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    yield


def _build():
    from hubzoid.factory_claude import build_claude_runtime
    return build_claude_runtime(MINIMAL)


def test_claude_runtime_disables_all_builtin_tools():
    """ClaudeAgentOptions.tools must be an empty list so the SDK does not
    expose Bash, Read, Edit, Write, Task, WebFetch, etc. The agent gets
    exactly the hubzoid MCP tools — nothing else."""
    runtime = _build()
    opts = runtime._options
    # tools=[] means "no built-ins". A None value or absence falls back to
    # the SDK's full claude_code preset — every built-in is available.
    assert opts.tools == [], (
        f"expected tools=[] to disable SDK built-ins; got {opts.tools!r}. "
        f"The agent will have Bash/Read/Task etc. and will ignore the "
        f"system addendum telling it not to escape."
    )


def test_claude_runtime_keeps_hubzoid_mcp_in_allowed_tools():
    """Sanity check the regression-guard above didn't accidentally also
    drop our MCP server from allowed_tools."""
    runtime = _build()
    opts = runtime._options
    assert any(t.startswith("mcp__hubzoid__") for t in opts.allowed_tools)


def test_claude_runtime_does_not_allow_builtin_tool_names_in_allowed_tools():
    """Belt-and-suspenders: even with tools=[], allowed_tools shouldn't
    list any SDK built-in by name (Bash, Read, Edit, Write, Task, etc.).
    Listing them there would re-enable them per the SDK semantics."""
    runtime = _build()
    builtins = {"Bash", "Read", "Edit", "Write", "Task", "Agent",
                "WebFetch", "WebSearch", "Grep", "Glob", "MultiEdit",
                "NotebookEdit", "NotebookRead"}
    leaked = builtins & set(runtime._options.allowed_tools)
    assert not leaked, f"built-ins leaked into allowed_tools: {leaked}"
