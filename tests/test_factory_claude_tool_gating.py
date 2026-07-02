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


# ---------------------------------------------------------------------------
# Model-delegate wiring: enabling the Agent spawn tool must be narrow — it
# must NOT re-admit Bash/Read/etc. And a hub with no delegates must keep the
# exact tools=[] gate (regression).
# ---------------------------------------------------------------------------
DELEGATE_HUB = FIXTURES / "delegate_claude_hub"


def test_no_delegates_keeps_tools_empty():
    """Regression: a hub with no delegates keeps the tools=[] gate exactly."""
    runtime = _build()  # MINIMAL, echo has no model
    assert runtime._options.tools == []
    assert not runtime._options.agents


def test_delegate_hub_enables_only_the_agent_spawn_tool(monkeypatch):
    monkeypatch.setenv("MODEL", "claude-local")  # hub = sonnet default
    from hubzoid.factory_claude import build_claude_runtime, SUBAGENT_SPAWN_TOOL
    runtime = build_claude_runtime(DELEGATE_HUB)
    opts = runtime._options
    # Only the spawn tool is enabled — Bash/Read/etc. stay off.
    assert opts.tools == [SUBAGENT_SPAWN_TOOL]
    assert SUBAGENT_SPAWN_TOOL in opts.allowed_tools
    # The delegate is registered as a native subagent on its own tier.
    assert "opus-helper" in opts.agents
    assert opts.agents["opus-helper"].model == "opus"
    # Its tool scope is the hubzoid MCP form of its whitelist.
    assert opts.agents["opus-helper"].tools == ["mcp__hubzoid__read_knowledge"]
    # hubzoid MCP tools still present in allowed_tools.
    assert any(t.startswith("mcp__hubzoid__") for t in opts.allowed_tools)


def test_delegate_hub_still_blocks_dangerous_builtins(monkeypatch):
    """Enabling Agent must NOT re-admit Bash/Read/Edit/Write etc."""
    monkeypatch.setenv("MODEL", "claude-local")
    from hubzoid.factory_claude import build_claude_runtime
    runtime = build_claude_runtime(DELEGATE_HUB)
    forbidden = {"Bash", "Read", "Edit", "Write", "WebFetch", "Grep", "Glob"}
    assert not (forbidden & set(runtime._options.tools))
    assert not (forbidden & set(runtime._options.allowed_tools))


def test_frontmatter_model_pins_main_tier_and_classification(tmp_path, monkeypatch):
    """With no .env MODEL, AGENTS.md `model:` must pin the main tier AND be the
    baseline for delegate classification (so a same-tier sub-agent stays a skill).
    """
    monkeypatch.delenv("MODEL", raising=False)
    (tmp_path / "AGENTS.md").write_text(
        "---\nname: m\ndescription: d\nmodel: claude-local/opus\n---\nbody"
    )
    # same tier as the (frontmatter) hub -> stays a skill, not a delegate.
    same = tmp_path / "agents" / "twin"
    same.mkdir(parents=True)
    (same / "AGENTS.md").write_text(
        "---\nname: twin\ndescription: d\nmodel: claude-local/opus\n---\nbody"
    )
    # different tier -> delegate.
    diff = tmp_path / "agents" / "speedy"
    diff.mkdir(parents=True)
    (diff / "AGENTS.md").write_text(
        "---\nname: speedy\ndescription: d\nmodel: claude-local/haiku\n---\nbody"
    )

    from hubzoid.factory_claude import build_claude_runtime
    runtime = build_claude_runtime(tmp_path)
    opts = runtime._options
    # Main agent actually runs opus (frontmatter honored, not None/CLI-default).
    assert opts.model == "opus"
    # twin (same tier as hub) is NOT a delegate; speedy is.
    assert "twin" not in (opts.agents or {})
    assert "speedy" in opts.agents
    assert opts.agents["speedy"].model == "haiku"
