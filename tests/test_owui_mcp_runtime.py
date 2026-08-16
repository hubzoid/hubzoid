"""ClaudeRuntime._options_for_turn merges the caller's OWUI MCP servers into
the per-turn options, and is a true no-op when there are none.
"""
from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from hubzoid import owui_mcp
from hubzoid.factory_claude import ClaudeRuntime


def _runtime(**opts):
    base = ClaudeAgentOptions(**opts)
    return base, ClaudeRuntime(name="t", options=base, hub_dir=Path("."))


def test_injects_owui_servers_and_preserves_base(monkeypatch):
    base, rt = _runtime(
        mcp_servers={"hubzoid": "SENTINEL"},
        allowed_tools=["mcp__hubzoid__whoami"],
    )
    monkeypatch.setattr(owui_mcp, "per_user_specs", lambda hub, ident: (
        {"owui_odoo": {"type": "http", "url": "https://m/mcp",
                       "headers": {"Authorization": "Bearer X"}}},
        ["mcp__owui_odoo__*"],
    ))
    turn = rt._options_for_turn()
    assert turn is not base                              # cloned, base untouched
    assert turn.mcp_servers["hubzoid"] == "SENTINEL"     # base server preserved
    assert turn.mcp_servers["owui_odoo"]["headers"]["Authorization"] == "Bearer X"
    assert "mcp__owui_odoo__*" in turn.allowed_tools
    assert "mcp__hubzoid__whoami" in turn.allowed_tools  # base glob preserved
    assert base.mcp_servers == {"hubzoid": "SENTINEL"}   # original not mutated


def test_noop_when_no_servers(monkeypatch):
    base, rt = _runtime(mcp_servers={"hubzoid": "S"}, allowed_tools=[])
    monkeypatch.setattr(owui_mcp, "per_user_specs", lambda hub, ident: ({}, []))
    assert rt._options_for_turn() is base                # identical object: no clone


def test_injection_error_is_swallowed(monkeypatch):
    base, rt = _runtime(mcp_servers={"hubzoid": "S"}, allowed_tools=[])

    def boom(hub, ident):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(owui_mcp, "per_user_specs", boom)
    # A DB/token hiccup must never break the turn -> falls back to base options.
    assert rt._options_for_turn() is base
