"""Scripted backend streams for the three runtimes (no model, no network).

Shared by ``tests/test_run_events.py`` and the recorded legacy text in
``tests/fixtures/run_events_legacy.json``. The recording was made by running
these scenarios through each runtime's ``stream()`` BEFORE the runtimes gained
``stream_events``, so the golden file is the 1.0.x text, byte for byte. Each
scenario drives the runtime through the same fake the existing tests use:

  * OpenAI Agents: ``agents.Runner.run_streamed`` returns a fake streaming
    result whose events are built from the steps.
  * Claude: ``claude_agent_sdk.query`` yields SDK messages built from the steps.
  * Codex: ``asyncio.create_subprocess_exec`` returns a fake app-server process
    that answers with the scripted JSON-RPC notifications.

Every scenario runs inside ``_request_ctx.chat_scope(CHAT_ID)`` so the download
footer (artifacts recorded during the turn) is part of the text.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hubzoid import _request_ctx

CHAT_ID = "chat-golden"
URL_A = "http://127.0.0.1:8000/artifacts/chat-golden/a.txt?t=aaaa"
URL_B = "http://127.0.0.1:8000/artifacts/chat-golden/b.txt?t=bbbb"

TOOL_MODES = ("off", "compact", "full")
THINKING_MODES = ("off", "indicator", "full")

GOLDEN = Path(__file__).parent / "fixtures" / "run_events_legacy.json"


def golden_key(runtime: str, scenario: str, tool_mode: str, thinking_mode: str = "-") -> str:
    return f"{runtime}/{scenario}/{tool_mode}/{thinking_mode}"


def load_golden() -> dict[str, str]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


async def _collect(agen, typed: bool) -> Any:
    items = [item async for item in agen]
    return items if typed else "".join(items)


# ---------------------------------------------------------------------------
# OpenAI Agents SDK
# ---------------------------------------------------------------------------
OPENAI_SCENARIOS: dict[str, list[tuple]] = {
    "text_only": [("delta", "Hello"), ("delta", " world")],
    "tool_then_text": [
        ("call", "read_knowledge", '{"name": "jexl"}', "call_1"),
        ("output", "call_1", "JEXL is an expression language."),
        ("delta", "Answer."),
    ],
    "tool_error_output": [
        ("call", "grep_data", '{"pattern": "x"}', "call_1"),
        ("output", "call_1", "An error occurred while running the tool. Please try again. Error: boom"),
        ("delta", "Sorry, that failed."),
    ],
    "fallback_message": [
        ("call", "read_knowledge", '{"name": "a"}', "call_1"),
        ("output", "call_1", "ok"),
        ("message", "Final answer without deltas."),
    ],
    "artifact_footer": [
        ("artifact", "a.txt", URL_A),
        ("delta", f"See [a.txt]({URL_A})."),
        ("artifact", "b.txt", URL_B),
    ],
    "exception": [
        ("delta", "Partial"),
        ("call", "read_knowledge", '{"name": "a"}', "call_1"),
        ("raise", "boom"),
    ],
    "mcp_prefixed_long_args": [
        ("call", "mcp__hubzoid__grep_data",
         json.dumps({"pattern": "`x`" + "y" * 60, "path": "raw_data/**/*.py", "more": 1}), "call_9"),
        ("output", "call_9", "no matches"),
        ("delta", "Nothing found."),
    ],
    "unparsable_args": [
        ("call", "read_knowledge", "{not json", "call_1"),
        ("output", "call_1", "ok"),
        ("delta", "Done."),
    ],
}


class _OpenAIResult:
    def __init__(self, steps):
        self._steps = steps
        self.context_wrapper = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=11, output_tokens=7, total_tokens=18, requests=1))
        self.cancelled = False

    def cancel(self, mode="immediate"):  # noqa: ARG002
        self.cancelled = True

    async def stream_events(self):
        from openai.types.responses import ResponseOutputText, ResponseTextDeltaEvent

        for n, step in enumerate(self._steps):
            kind = step[0]
            if kind == "delta":
                yield SimpleNamespace(type="raw_response_event", data=ResponseTextDeltaEvent(
                    content_index=0, delta=step[1], item_id="msg", logprobs=[], output_index=0,
                    sequence_number=n, type="response.output_text.delta"))
            elif kind == "call":
                _, name, args, call_id = step
                raw = SimpleNamespace(name=name, arguments=args, call_id=call_id,
                                      type="function_call")
                yield SimpleNamespace(type="run_item_stream_event", name="tool_called",
                                      item=SimpleNamespace(type="tool_call_item", raw_item=raw,
                                                           call_id=call_id))
            elif kind == "output":
                _, call_id, output = step
                raw = {"call_id": call_id, "output": output, "type": "function_call_output"}
                yield SimpleNamespace(type="run_item_stream_event", name="tool_output",
                                      item=SimpleNamespace(type="tool_call_output_item",
                                                           raw_item=raw, output=output,
                                                           call_id=call_id))
            elif kind == "message":
                raw = SimpleNamespace(content=[ResponseOutputText(
                    annotations=[], text=step[1], type="output_text")])
                yield SimpleNamespace(type="run_item_stream_event", name="message_output_created",
                                      item=SimpleNamespace(type="message_output_item", raw_item=raw))
            elif kind == "artifact":
                _request_ctx.record_artifact(step[1], step[2])
            elif kind == "raise":
                raise RuntimeError(step[1])


def run_openai(mp, scenario: str, *, tool_mode: str, typed: bool = False):
    """Drive OpenAIAgentsRuntime through `scenario`. Returns the text of
    `stream()` or, with `typed`, the items of `stream_events()`."""
    import agents

    from hubzoid.runtime import OpenAIAgentsRuntime

    steps = OPENAI_SCENARIOS[scenario]
    mp.setattr(agents.Runner, "run_streamed",
               lambda agent, run_input, max_turns=None: _OpenAIResult(steps))
    rt = OpenAIAgentsRuntime(SimpleNamespace(name="golden", mcp_servers=[], model="golden-model"),
                             tool_mode=tool_mode, hub_dir=None)

    async def go():
        with _request_ctx.chat_scope(CHAT_ID):
            agen = rt.stream_events("hi") if typed else rt.stream("hi")
            return await _collect(agen, typed)

    return asyncio.run(go())


# ---------------------------------------------------------------------------
# Claude Agent SDK
# ---------------------------------------------------------------------------
CLAUDE_SCENARIOS: dict[str, list[tuple]] = {
    "text_only": [("text", "Hello"), ("text", " there"), ("result", False, "success", "Hello there")],
    "think_then_text": [
        ("think", "Let me"), ("think", " consider."), ("text", "Answer"),
        ("result", False, "success", "Answer"),
    ],
    "think_tool_think_text": [
        ("think", "plan"),
        ("tool_use", "t1", "mcp__hubzoid__read_knowledge", {"name": "jexl"}),
        ("tool_result", "t1", False),
        ("think", "more"),
        ("text", "Done"),
        ("result", False, "success", "Done"),
    ],
    "tool_error": [
        ("tool_use", "t1", "mcp__hubzoid__grep_data", {"pattern": "x"}),
        ("tool_result", "t1", True),
        ("text", "It failed."),
        ("result", False, "success", "It failed."),
    ],
    "think_then_tool_error": [
        ("think", "a"),
        ("tool_use", "t1", "mcp__hubzoid__grep_data", {"pattern": "x"}),
        ("think", "b"),
        ("tool_result", "t1", True),
        ("text", "x"),
        ("result", False, "success", "x"),
    ],
    "fallback_result": [
        ("tool_use", "t1", "mcp__hubzoid__read_knowledge", {}),
        ("tool_result", "t1", False),
        ("result", False, "success", "Final from the result message."),
    ],
    "sdk_error": [
        ("think", "a"), ("text", "partial"), ("result", True, "error_max_turns", None),
    ],
    "exception": [("think", "a"), ("raise", "boom")],
    "artifact_footer": [
        ("artifact", "a.txt", URL_A),
        ("text", f"Saved [a.txt]({URL_A})."),
        ("artifact", "b.txt", URL_B),
        ("result", False, "success", "Saved."),
    ],
    "duplicate_tool_use": [
        ("tool_use", "t1", "mcp__hubzoid__read_knowledge", {"name": "a"}),
        ("tool_use", "t1", "mcp__hubzoid__read_knowledge", {"name": "a"}),
        ("tool_result", "t1", False),
        ("text", "ok"),
        ("result", False, "success", "ok"),
    ],
    "think_only": [("think", "only thinking"), ("result", False, "success", None)],
    "empty_thinking_delta": [
        ("think", ""), ("think", "x"), ("text", "y"), ("result", False, "success", "y"),
    ],
    "unknown_result_id": [
        ("tool_result", "zz", True), ("text", "z"), ("result", False, "success", "z"),
    ],
}


class _ClaudeOptions:
    system_prompt = "sys"
    mcp_servers: dict = {}


def _claude_messages(steps):
    from claude_agent_sdk import AssistantMessage, ResultMessage, UserMessage
    from claude_agent_sdk.types import StreamEvent, ToolResultBlock, ToolUseBlock

    async def gen():
        for step in steps:
            kind = step[0]
            if kind == "think":
                yield StreamEvent(uuid="u", session_id="s", event={
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": step[1]}})
            elif kind == "text":
                yield StreamEvent(uuid="u", session_id="s", event={
                    "type": "content_block_delta", "delta": {"type": "text_delta", "text": step[1]}})
            elif kind == "tool_use":
                _, tid, name, args = step
                yield AssistantMessage(content=[ToolUseBlock(id=tid, name=name, input=args)],
                                       model="claude-golden")
            elif kind == "tool_result":
                _, tid, is_error = step
                yield UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="r",
                                                           is_error=is_error)])
            elif kind == "result":
                _, is_error, subtype, result = step
                yield ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1,
                                    is_error=is_error, num_turns=1, session_id="s",
                                    result=result, usage={"input_tokens": 3, "output_tokens": 2})
            elif kind == "artifact":
                _request_ctx.record_artifact(step[1], step[2])
            elif kind == "raise":
                raise RuntimeError(step[1])

    return gen()


def run_claude(mp, scenario: str, *, tool_mode: str, thinking_mode: str, typed: bool = False):
    import claude_agent_sdk

    from hubzoid.factory_claude import ClaudeRuntime

    steps = CLAUDE_SCENARIOS[scenario]
    mp.setattr(claude_agent_sdk, "query",
               lambda *, prompt, options: _claude_messages(steps), raising=False)
    rt = ClaudeRuntime(name="golden", options=_ClaudeOptions(), thinking_mode=thinking_mode,
                       tool_mode=tool_mode, hub_dir=None)
    rt._options_for_turn = lambda: _ClaudeOptions()

    async def go():
        with _request_ctx.chat_scope(CHAT_ID):
            agen = rt.stream_events("hi") if typed else rt.stream("hi")
            return await _collect(agen, typed)

    return asyncio.run(go())


# ---------------------------------------------------------------------------
# Codex app-server
# ---------------------------------------------------------------------------
def _codex_call(tool: str, call_id: str, args: dict | None = None, rpc_id: int = 10) -> dict:
    return {"method": "item/tool/call", "id": rpc_id,
            "params": {"tool": tool, "arguments": args or {}, "callId": call_id,
                       "threadId": "thread"}}


def _delta(text: str, item: str = "m1") -> dict:
    return {"method": "item/agentMessage/delta", "params": {"itemId": item, "delta": text}}


def _completed(text: str, item: str = "m1") -> dict:
    return {"method": "item/completed",
            "params": {"item": {"type": "agentMessage", "id": item, "text": text}}}


CODEX_SCENARIOS: dict[str, list[dict]] = {
    "text_only": [_delta("Hel"), _delta("lo"), _completed("Hello")],
    "tool_ok": [_codex_call("lookup", "c1", {"q": "x"}), _delta("Answer.")],
    "tool_failure": [_codex_call("broken", "c1", {"q": "x"}), _delta("Sorry.")],
    "unknown_tool": [_codex_call("nope", "c1"), _delta("ok")],
    "completed_fallback": [_completed("Full text without deltas.", item="m2")],
    "turn_failed": [_delta("Partial"),
                    {"method": "turn/completed",
                     "params": {"threadId": "thread", "turn": {"status": "failed"}}}],
    "artifact_footer": [_codex_call("saver", "c1", {"name": "b.txt"}), _delta("Saved.")],
}


class _CodexProcess:
    returncode = None
    pid = 424242

    def __init__(self, events):
        self.stdout = asyncio.StreamReader()
        for event in events:
            self.stdout.feed_data((json.dumps(event) + "\n").encode())
        self.stdout.feed_eof()
        self.stdin = self
        self.messages: list[dict] = []

    def write(self, value):
        self.messages.append(json.loads(value))

    async def drain(self):
        pass


def _codex_events(middle: list[dict]) -> list[dict]:
    return [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "thread"}, "model": "codex-golden"}},
        *middle,
        {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"status": "completed"}}},
    ]


def codex_registry() -> dict:
    from agents import function_tool

    @function_tool
    def lookup(q: str) -> str:
        """Look something up."""
        return f"found {q}"

    @function_tool(failure_error_function=None)
    def broken(q: str) -> str:
        """Always fails."""
        raise RuntimeError(f"broken {q}")

    @function_tool
    def saver(name: str) -> str:
        """Save a file."""
        _request_ctx.record_artifact(name, URL_B)
        return "saved"

    return {"lookup": lookup, "broken": broken, "saver": saver}


def run_codex(mp, tmp_path: Path, scenario: str, *, tool_mode: str, typed: bool = False):
    from hubzoid import factory_codex
    from hubzoid.factory_codex import CodexRuntime

    auth = tmp_path / "codex-auth.json"
    auth.write_text("{}")
    mp.setattr(factory_codex, "_auth_file", lambda: auth)
    mp.setattr(factory_codex, "codex_binary", lambda: "codex")

    async def stop(proc):
        proc.returncode = 0

    mp.setattr(factory_codex, "_stop", stop)
    events = _codex_events(CODEX_SCENARIOS[scenario])

    async def spawn(*args, **kwargs):  # noqa: ARG001
        return _CodexProcess(events)

    mp.setattr(asyncio, "create_subprocess_exec", spawn)
    rt = CodexRuntime(name="golden", instructions="", registry=codex_registry(),
                      tool_mode=tool_mode)

    async def go():
        with _request_ctx.chat_scope(CHAT_ID):
            agen = rt.stream_events("hi") if typed else rt.stream("hi")
            return await _collect(agen, typed)

    return asyncio.run(go())


def all_cases():
    """(runtime, scenario, tool_mode, thinking_mode) for every recorded case."""
    for scenario in OPENAI_SCENARIOS:
        for tool_mode in TOOL_MODES:
            yield "openai", scenario, tool_mode, "-"
    for scenario in CLAUDE_SCENARIOS:
        for tool_mode in TOOL_MODES:
            for thinking_mode in THINKING_MODES:
                yield "claude", scenario, tool_mode, thinking_mode
    for scenario in CODEX_SCENARIOS:
        for tool_mode in TOOL_MODES:
            yield "codex", scenario, tool_mode, "-"


def run_case(mp, tmp_path: Path, runtime: str, scenario: str, tool_mode: str,
             thinking_mode: str, *, typed: bool = False):
    if runtime == "openai":
        return run_openai(mp, scenario, tool_mode=tool_mode, typed=typed)
    if runtime == "claude":
        return run_claude(mp, scenario, tool_mode=tool_mode, thinking_mode=thinking_mode,
                          typed=typed)
    return run_codex(mp, tmp_path, scenario, tool_mode=tool_mode, typed=typed)
