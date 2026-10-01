"""Typed run events.

Every runtime's ``stream_events`` carries the structure the web app renders
(tool calls and results, reasoning, notices). The text boundary renders tool
activity from those events; ``stream()`` and the adapted typed stream match the
recorded text (tests/fixtures/run_events_legacy.json) for every scenario and
every SHOW_TOOLS / SHOW_THINKING mode.

``run_once`` (a workflow's ``hub.call_agent``) keeps only the answer.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from hubzoid import _request_ctx, run_events, tool_events
from hubzoid.run_events import (
    Notice,
    ReasoningDelta,
    ReasoningEnd,
    RunEvent,
    ToolCall,
    ToolResult,
)
from tests import run_event_scenarios as scenarios

GOLDEN = scenarios.load_golden()
CASES = list(scenarios.all_cases())
IDS = [scenarios.golden_key(*case) for case in CASES]


def test_every_recorded_case_is_covered():
    assert sorted(IDS) == sorted(GOLDEN)


# ---------------------------------------------------------------------------
# Byte-identical 1.0.x text
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_stream_text_is_unchanged(case, monkeypatch, tmp_path):
    assert scenarios.run_case(monkeypatch, tmp_path, *case) == GOLDEN[scenarios.golden_key(*case)]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_typed_stream_renders_the_same_text(case, monkeypatch, tmp_path):
    items = scenarios.run_case(monkeypatch, tmp_path, *case, typed=True)
    assert all(isinstance(item, (str, RunEvent)) for item in items)
    async def typed_items():
        for item in items:
            yield item

    async def rendered():
        return "".join([chunk async for chunk in run_events.as_text(
            typed_items(), tool_mode=case[2])])

    assert asyncio.run(rendered()) == GOLDEN[scenarios.golden_key(*case)]


def test_as_text_drops_empty_items():
    async def items():
        yield ""
        yield ToolCall(id="c", name="t", legacy="")
        yield "a"
        yield Notice(kind="artifacts", text="x", legacy="\n\nx")

    async def go():
        return [t async for t in run_events.as_text(items())]

    assert asyncio.run(go()) == ["a", "\n\nx"]


def test_answer_text_keeps_only_strings():
    assert run_events.answer_text("hi") == "hi"
    assert run_events.answer_text(Notice(kind="error", text="x", legacy="[agent error]")) == ""
    assert run_events.answer_text(ReasoningDelta(text="t", legacy="<think>t")) == ""


# ---------------------------------------------------------------------------
# OpenAI Agents SDK
# ---------------------------------------------------------------------------
def test_openai_tool_call_then_result(monkeypatch):
    call, result, answer = scenarios.run_openai(monkeypatch, "tool_then_text",
                                                tool_mode="compact", typed=True)
    assert isinstance(call, ToolCall)
    assert (call.id, call.name, call.args) == ("call_1", "read_knowledge", {"name": "jexl"})
    assert call.legacy == tool_events.format_call("read_knowledge", {"name": "jexl"}, mode="compact")
    assert result == ToolResult(id="call_1", name="read_knowledge", ok=True)
    assert result.legacy == ""
    assert answer == "Answer."


def test_openai_tool_calls_are_typed_even_when_hidden(monkeypatch):
    items = scenarios.run_openai(monkeypatch, "tool_then_text", tool_mode="off", typed=True)
    assert [type(i) for i in items] == [ToolCall, ToolResult, str]
    assert items[0].legacy == ""


def test_openai_failed_tool_is_an_error_result_without_legacy_text(monkeypatch):
    items = scenarios.run_openai(monkeypatch, "tool_error_output", tool_mode="full", typed=True)
    result = next(i for i in items if isinstance(i, ToolResult))
    assert result.ok is False and result.message and result.legacy == ""


def test_openai_prefixed_tool_name_is_shortened(monkeypatch):
    items = scenarios.run_openai(monkeypatch, "mcp_prefixed_long_args", tool_mode="full", typed=True)
    call = next(i for i in items if isinstance(i, ToolCall))
    assert call.name == "grep_data" and call.id == "call_9"
    assert isinstance(call.args, dict) and call.args["path"] == "raw_data/**/*.py"


def test_openai_footer_and_error_are_notices(monkeypatch):
    footer = scenarios.run_openai(monkeypatch, "artifact_footer", tool_mode="off", typed=True)[-1]
    assert isinstance(footer, Notice) and footer.kind == "artifacts"
    assert scenarios.URL_B in footer.text and scenarios.URL_A not in footer.text
    error = scenarios.run_openai(monkeypatch, "exception", tool_mode="off", typed=True)[-1]
    assert error == Notice(kind="error", text="RuntimeError: boom",
                           legacy="\n\n[agent error: RuntimeError: boom]")


def test_openai_stream_closed_early_cancels_the_model_run(monkeypatch):
    import agents
    from openai.types.responses import ResponseTextDeltaEvent

    from hubzoid.runtime import OpenAIAgentsRuntime

    class Result:
        cancelled = False
        context_wrapper = None

        def cancel(self, mode="immediate"):  # noqa: ARG002
            Result.cancelled = True

        async def stream_events(self):
            yield SimpleNamespace(type="raw_response_event", data=ResponseTextDeltaEvent(
                content_index=0, delta="first", item_id="m", logprobs=[], output_index=0,
                sequence_number=0, type="response.output_text.delta"))
            await asyncio.Event().wait()

    monkeypatch.setattr(agents.Runner, "run_streamed", lambda *a, **k: Result())
    rt = OpenAIAgentsRuntime(SimpleNamespace(name="t", mcp_servers=[], model="m"))

    async def go():
        stream = rt.stream_events("hi")
        assert await stream.__anext__() == "first"
        await stream.aclose()

    asyncio.run(go())
    assert Result.cancelled


# ---------------------------------------------------------------------------
# Claude Agent SDK
# ---------------------------------------------------------------------------
def test_claude_full_reasoning_tools_and_text(monkeypatch):
    items = scenarios.run_claude(monkeypatch, "think_tool_think_text", tool_mode="compact",
                                 thinking_mode="full", typed=True)
    assert items == [
        ReasoningDelta(text="plan", legacy="<think>\nplan"),
        ReasoningEnd(legacy="\n</think>\n"),
        ToolCall(id="t1", name="read_knowledge", args={"name": "jexl"},
                 legacy=tool_events.format_call("read_knowledge", {"name": "jexl"}, mode="compact")),
        ToolResult(id="t1", name="read_knowledge", ok=True),
        ReasoningDelta(text="more", legacy="<think>\nmore"),
        ReasoningEnd(legacy="\n</think>\n"),
        "Done",
    ]


def test_claude_indicator_mode_hides_reasoning_text(monkeypatch):
    items = scenarios.run_claude(monkeypatch, "think_then_text", tool_mode="compact",
                                 thinking_mode="indicator", typed=True)
    assert items == [
        ReasoningDelta(text="", legacy="<think>\n"),
        ReasoningEnd(legacy="\n</think>\n"),
        "Answer",
    ]


def test_claude_off_mode_has_no_reasoning(monkeypatch):
    items = scenarios.run_claude(monkeypatch, "think_tool_think_text", tool_mode="full",
                                 thinking_mode="off", typed=True)
    assert not [i for i in items if isinstance(i, (ReasoningDelta, ReasoningEnd))]


def test_claude_reasoning_resuming_in_an_open_block_is_signalled(monkeypatch):
    """With SHOW_TOOLS=off a tool call prints nothing, so 1.0.x kept the
    <think> block open across it. The web app still hears that reasoning resumed."""
    items = scenarios.run_claude(monkeypatch, "think_tool_think_text", tool_mode="off",
                                 thinking_mode="indicator", typed=True)
    kinds = [type(i).__name__ for i in items]
    assert kinds == ["ReasoningDelta", "ToolCall", "ToolResult", "ReasoningDelta",
                     "ReasoningEnd", "str"]
    assert items[3] == ReasoningDelta(text="", legacy="")


def test_claude_tool_error_result_carries_the_warning_line(monkeypatch):
    items = scenarios.run_claude(monkeypatch, "tool_error", tool_mode="off",
                                 thinking_mode="off", typed=True)
    result = next(i for i in items if isinstance(i, ToolResult))
    assert result.ok is False and result.name == "grep_data" and result.message
    assert result.legacy == tool_events.format_error("grep_data")


def test_claude_errors_and_footer_are_notices(monkeypatch):
    sdk = scenarios.run_claude(monkeypatch, "sdk_error", tool_mode="off",
                               thinking_mode="off", typed=True)[-1]
    assert sdk == Notice(kind="error", text="claude run ended with error_max_turns",
                         legacy="\n\n[agent error: claude run ended with error_max_turns]")
    footer = scenarios.run_claude(monkeypatch, "artifact_footer", tool_mode="off",
                                  thinking_mode="off", typed=True)[-1]
    assert footer.kind == "artifacts" and scenarios.URL_B in footer.text


def test_claude_stream_closed_early_closes_the_sdk_query(monkeypatch):
    import claude_agent_sdk
    from claude_agent_sdk.types import StreamEvent

    from hubzoid.factory_claude import ClaudeRuntime

    closed = []

    async def query(*, prompt, options):  # noqa: ARG001
        try:
            yield StreamEvent(uuid="u", session_id="s", event={
                "type": "content_block_delta", "delta": {"type": "text_delta", "text": "first"}})
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr(claude_agent_sdk, "query", query, raising=False)
    rt = ClaudeRuntime(name="t", options=scenarios._ClaudeOptions(), hub_dir=None)
    rt._options_for_turn = lambda: scenarios._ClaudeOptions()

    async def go():
        stream = rt.stream_events("hi")
        assert await stream.__anext__() == "first"
        await stream.aclose()

    asyncio.run(go())
    assert closed == [True]


# ---------------------------------------------------------------------------
# Codex app-server
# ---------------------------------------------------------------------------
def test_codex_tool_call_then_result(monkeypatch, tmp_path):
    items = scenarios.run_codex(monkeypatch, tmp_path, "tool_ok", tool_mode="full", typed=True)
    assert items[0] == ToolCall(id="c1", name="lookup", args={"q": "x"},
                                legacy=tool_events.format_call("lookup", {"q": "x"}, mode="full"))
    assert items[1] == ToolResult(id="c1", name="lookup", ok=True)
    assert items[2:] == ["Answer."]


def test_codex_failed_tool_prints_nothing_but_is_typed(monkeypatch, tmp_path):
    items = scenarios.run_codex(monkeypatch, tmp_path, "tool_failure", tool_mode="compact", typed=True)
    result = next(i for i in items if isinstance(i, ToolResult))
    assert result.ok is False and result.message and result.legacy == ""


def test_codex_unknown_tool_is_not_disclosed(monkeypatch, tmp_path):
    items = scenarios.run_codex(monkeypatch, tmp_path, "unknown_tool", tool_mode="full", typed=True)
    assert items == ["ok"]


def test_codex_failure_and_footer_are_notices(monkeypatch, tmp_path):
    failed = scenarios.run_codex(monkeypatch, tmp_path, "turn_failed", tool_mode="off", typed=True)
    assert isinstance(failed[-1], Notice) and failed[-1].kind == "error"
    assert failed[-1].legacy.startswith("\n\n[Codex could not complete this request: ")
    saved = scenarios.run_codex(monkeypatch, tmp_path, "artifact_footer", tool_mode="off", typed=True)
    assert isinstance(saved[-1], Notice) and saved[-1].kind == "artifacts"


def test_codex_direct_exchange_still_yields_text():
    """A caller of `_exchange` itself (outside a turn) keeps the 1.0.x text."""
    from hubzoid.factory_codex import CodexRuntime

    rt = CodexRuntime(name="t", instructions="", registry=scenarios.codex_registry(), tool_mode="full")

    async def go():
        proc = scenarios._CodexProcess(scenarios._codex_events(scenarios.CODEX_SCENARIOS["tool_ok"]))
        return [part async for part in rt._exchange(proc, "hi", "/tmp", {})]

    parts = asyncio.run(go())
    assert all(isinstance(p, str) for p in parts)
    assert "".join(parts) == GOLDEN["codex/tool_ok/full/-"]


# ---------------------------------------------------------------------------
# stream_items: runtimes without stream_events
# ---------------------------------------------------------------------------
def test_stream_items_falls_back_to_text():
    class TextOnly:
        async def stream(self, prompt):
            yield f"echo {prompt}"

    async def go():
        return [i async for i in run_events.stream_items(TextOnly(), "x")]

    assert asyncio.run(go()) == ["echo x"]


# ---------------------------------------------------------------------------
# run_once: the answer only (workflow reports never get chat markup)
# ---------------------------------------------------------------------------
@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = tmp_path / "wf-hub"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: wf\ndescription: d\n---\nHelp.\n")
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    return hub


def test_run_once_returns_only_the_answer_openai(hub, monkeypatch):
    """Regression: 1.0.x returned the chat text, so tool entries, the download
    footer and error decorations leaked into workflow reports."""
    import agents

    from hubzoid import runtime

    monkeypatch.setenv("MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("SHOW_TOOLS", "full")
    steps = [("call", "read_knowledge", '{"name": "a"}', "call_1"),
             ("output", "call_1", "ok"),
             ("delta", "The answer "), ("delta", "is 42."),
             ("artifact", "b.txt", scenarios.URL_B)]
    monkeypatch.setattr(agents.Runner, "run_streamed",
                        lambda agent, run_input, max_turns=None: scenarios._OpenAIResult(steps))
    assert runtime.run_once(hub, "compute") == "The answer is 42."


def test_run_once_returns_only_the_answer_claude(hub, monkeypatch):
    import claude_agent_sdk

    from hubzoid import runtime

    monkeypatch.setenv("MODEL", "claude-local")
    monkeypatch.setenv("SHOW_THINKING", "full")
    steps = scenarios.CLAUDE_SCENARIOS["think_tool_think_text"]
    monkeypatch.setattr(claude_agent_sdk, "query",
                        lambda *, prompt, options: scenarios._claude_messages(steps), raising=False)
    assert runtime.run_once(hub, "compute") == "Done"


def test_run_once_still_raises_on_a_failed_run(hub, monkeypatch):
    import claude_agent_sdk

    from hubzoid import runtime

    monkeypatch.setenv("MODEL", "claude-local")
    steps = scenarios.CLAUDE_SCENARIOS["sdk_error"]
    monkeypatch.setattr(claude_agent_sdk, "query",
                        lambda *, prompt, options: scenarios._claude_messages(steps), raising=False)
    with pytest.raises(runtime.AgentRunError, match="error_max_turns"):
        runtime.run_once(hub, "compute")


def test_run_once_scripted_runtime_answer_only(hub, monkeypatch):
    from hubzoid import runtime

    monkeypatch.setenv("MODEL", "hubzoid-test/scripted")
    monkeypatch.setenv("HUBZOID_TEST_RUNTIME", "1")
    monkeypatch.setenv("SHOW_THINKING", "full")
    answer = runtime.run_once(hub, "think, use a tool, then show markdown")
    assert answer.startswith("Here is a summary table:")
    assert "<think>" not in answer and "read_knowledge" not in answer
    with pytest.raises(runtime.AgentRunError, match="scripted failure"):
        runtime.run_once(hub, "please error")


def test_usage_is_still_recorded_for_the_turn(hub, monkeypatch):
    """The typed stream keeps each backend's usage report (drained by callers)."""
    items = scenarios.run_openai(monkeypatch, "text_only", tool_mode="off", typed=True)
    assert items == ["Hello", " world"]

    async def go():
        with _request_ctx.chat_scope("c"):
            import agents

            monkeypatch.setattr(agents.Runner, "run_streamed",
                                lambda *a, **k: scenarios._OpenAIResult(
                                    scenarios.OPENAI_SCENARIOS["text_only"]))
            from hubzoid.runtime import OpenAIAgentsRuntime

            rt = OpenAIAgentsRuntime(SimpleNamespace(name="t", mcp_servers=[], model="m"))
            _ = [i async for i in rt.stream_events("hi")]
            return _request_ctx.drain_usage()

    usage = asyncio.run(go())
    assert usage["input_tokens"] == 11 and usage["output_tokens"] == 7 and usage["model"] == "m"


def test_golden_file_is_valid_json():
    assert json.loads(scenarios.GOLDEN.read_text(encoding="utf-8")) == GOLDEN
