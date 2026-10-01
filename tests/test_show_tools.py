"""SHOW_TOOLS: how tool-call activity is surfaced.

compact (default) -> collapsible dropdown on web, hidden on Slack.
full              -> legacy inline blockquote on every surface.
off               -> emit nothing.

Mirrors the SHOW_THINKING knob (see test_thinking_indicator.py).
"""
from __future__ import annotations

from hubzoid import reasoning
from hubzoid import settings as settingslib


# --- normalize_tools: off | compact | full, default compact ----------------
def test_normalize_tools_default_is_compact():
    assert reasoning.normalize_tools(None) == "compact"
    assert reasoning.normalize_tools("") == "compact"


def test_normalize_tools_passes_canonical_values():
    assert reasoning.normalize_tools("off") == "off"
    assert reasoning.normalize_tools("compact") == "compact"
    assert reasoning.normalize_tools("full") == "full"


def test_normalize_tools_is_case_insensitive():
    assert reasoning.normalize_tools("  COMPACT ") == "compact"


def test_normalize_tools_off_aliases():
    for alias in ("false", "none", "no", "0", "hide", "hidden", "disabled"):
        assert reasoning.normalize_tools(alias) == "off"


def test_normalize_tools_full_aliases():
    for alias in ("inline", "legacy", "blockquote", "verbose"):
        assert reasoning.normalize_tools(alias) == "full"


def test_normalize_tools_unknown_falls_back_to_compact():
    assert reasoning.normalize_tools("banana") == "compact"


# --- settings.load wires SHOW_TOOLS onto Settings.show_tools ---------------
def test_settings_default_show_tools_is_compact(tmp_path, monkeypatch):
    monkeypatch.delenv("SHOW_TOOLS", raising=False)
    s = settingslib.load(tmp_path)
    assert s.show_tools == "compact"


def test_settings_reads_show_tools_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOW_TOOLS", "off")
    s = settingslib.load(tmp_path)
    assert s.show_tools == "off"


def test_openai_runtime_shows_a_status_then_finished_blocks(monkeypatch):
    """OpenAI Agents SDK: tool_call_item starts a call, tool_call_output_item
    finishes it (✗ for the SDK's own failure text), paired by call_id."""
    import asyncio
    from types import SimpleNamespace as NS

    import agents

    from hubzoid.runtime import OpenAIAgentsRuntime
    from hubzoid.tool_events import Status

    def item(kind, **kw):
        return NS(type="run_item_stream_event", item=NS(type=kind, **kw))

    events = [
        item("tool_call_item", raw_item=NS(name="check_program", arguments='{"event_id": 1556}',
                                           call_id="call_1")),
        item("tool_call_item", raw_item=NS(name="finance_review_report", arguments="{}",
                                           call_id="call_2")),
        item("tool_call_output_item", raw_item={"call_id": "call_1"}, output="ok"),
        item("tool_call_output_item", raw_item={"call_id": "call_2"},
             output="An error occurred while running the tool. Please try again. Error: down"),
    ]

    class _Result:
        def stream_events(self):
            async def gen():
                for e in events:
                    yield e
            return gen()

    monkeypatch.setattr(agents.Runner, "run_streamed",
                        lambda agent, prompt, max_turns=None: _Result())
    rt = OpenAIAgentsRuntime(NS(name="t", mcp_servers=[]), tool_mode="compact")

    async def collect():
        return [p async for p in rt.stream("hi")]

    parts = asyncio.run(collect())
    assert [p.description for p in parts if isinstance(p, Status)] == [
        "Running check_program…", "Running check_program and finance_review_report…",
        "Running finance_review_report…", None]
    text = "".join(parts)
    assert 'id="call_1" name="check_program" arguments="{&quot;event_id&quot;: 1556}">' in text
    assert 'id="call_2" name="finance_review_report" arguments="{}" status="failed"' in text
