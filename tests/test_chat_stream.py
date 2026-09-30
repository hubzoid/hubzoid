"""The AI SDK UI message stream (contract 6.3) and stored content (6.4) built
from one run's typed items."""
from __future__ import annotations

from hubzoid.chat.stream import DONE, HEADERS, MessageBuilder, encode, tool_input
from hubzoid.run_events import Notice, ReasoningDelta, ReasoningEnd, ToolCall, ToolResult


def _run(items, *, status="complete", show_tools=True, title=None):
    b = MessageBuilder("m_reply0001", "c_conv00001", show_tools=show_tools)
    chunks = b.start()
    if title:
        chunks += b.title(title)
    for item in items:
        chunks += b.feed(item)
    chunks += b.finish(status)
    return b, chunks


GOLDEN_ITEMS = [
    ReasoningDelta(text="Let me look.", legacy="<think>\nLet me look."),
    ReasoningEnd(legacy="\n</think>\n"),
    ToolCall(id="call_1", name="read_knowledge", args={"name": "jexl"}, legacy="x"),
    ToolResult(id="call_1", name="read_knowledge", ok=True),
    "JEXL is ",
    "an expression language.",
    ToolCall(id="call_2", name="grep_data", args='{"pattern": "a"}', legacy="y"),
    ToolResult(id="call_2", name="grep_data", ok=False, message="The tool did not complete."),
    "Done.",
    Notice(kind="artifacts", text="\n\n[Download a.txt](http://h/a)\n",
           legacy="\n\n[Download a.txt](http://h/a)\n"),
]

GOLDEN_WIRE = (
    'data: {"type":"start","messageId":"m_reply0001","messageMetadata":{"conversationId":"c_conv00001"}}\n\n'
    'data: {"type":"start-step"}\n\n'
    'data: {"type":"data-title","data":{"title":"JEXL question"},"transient":true}\n\n'
    'data: {"type":"reasoning-start","id":"r1"}\n\n'
    'data: {"type":"reasoning-delta","id":"r1","delta":"Let me look."}\n\n'
    'data: {"type":"reasoning-end","id":"r1"}\n\n'
    'data: {"type":"tool-input-available","toolCallId":"call_1","toolName":"read_knowledge","input":{"name":"jexl"}}\n\n'
    'data: {"type":"tool-output-available","toolCallId":"call_1","output":{"status":"ok"}}\n\n'
    'data: {"type":"text-start","id":"t1"}\n\n'
    'data: {"type":"text-delta","id":"t1","delta":"JEXL is "}\n\n'
    'data: {"type":"text-delta","id":"t1","delta":"an expression language."}\n\n'
    'data: {"type":"text-end","id":"t1"}\n\n'
    'data: {"type":"tool-input-available","toolCallId":"call_2","toolName":"grep_data","input":{"pattern":"a"}}\n\n'
    'data: {"type":"tool-output-error","toolCallId":"call_2","errorText":"The tool did not complete."}\n\n'
    'data: {"type":"text-start","id":"t2"}\n\n'
    'data: {"type":"text-delta","id":"t2","delta":"Done."}\n\n'
    'data: {"type":"text-delta","id":"t2","delta":"\\n\\n[Download a.txt](http://h/a)\\n"}\n\n'
    'data: {"type":"text-end","id":"t2"}\n\n'
    'data: {"type":"finish-step"}\n\n'
    'data: {"type":"finish","messageMetadata":{"status":"complete"}}\n\n'
    'data: [DONE]\n\n'
)


def test_wire_format_golden():
    _, chunks = _run(GOLDEN_ITEMS, title="JEXL question")
    wire = b"".join(encode(c) for c in chunks) + DONE
    assert wire.decode("utf-8") == GOLDEN_WIRE


def test_stream_header():
    assert HEADERS["x-vercel-ai-ui-message-stream"] == "v1"


def test_stored_parts_match_the_stream():
    b, _ = _run(GOLDEN_ITEMS)
    assert b.snapshot() == [
        {"type": "reasoning", "text": "Let me look."},
        {"type": "tool-call", "toolCallId": "call_1", "toolName": "read_knowledge",
         "args": {"name": "jexl"}, "result": {"status": "ok"}},
        {"type": "text", "text": "JEXL is an expression language."},
        {"type": "tool-call", "toolCallId": "call_2", "toolName": "grep_data",
         "args": {"pattern": "a"},
         "result": {"status": "error", "message": "The tool did not complete."}},
        {"type": "text", "text": "Done.\n\n[Download a.txt](http://h/a)\n"},
    ]
    assert b.plain_text() == "JEXL is an expression language.Done.\n\n[Download a.txt](http://h/a)\n"


def test_indicator_reasoning_has_a_part_without_text():
    b, chunks = _run([ReasoningDelta(text="", legacy="<think>\n_Thinking…_"),
                      ReasoningEnd(legacy="\n</think>\n"), "Answer"])
    kinds = [c["type"] for c in chunks]
    assert kinds[2:5] == ["reasoning-start", "reasoning-end", "text-start"]
    assert "reasoning-delta" not in kinds
    assert b.snapshot()[0] == {"type": "reasoning", "text": ""}


def test_reasoning_resuming_after_text_opens_a_new_block():
    b, chunks = _run(["a", ReasoningDelta(text="b"), "c", ReasoningDelta(text="d")])
    ids = [c.get("id") for c in chunks if c["type"].endswith("-start") and c["type"] != "start"]
    assert ids == ["t1", "r1", "t2", "r2"]
    assert [p["type"] for p in b.snapshot()] == ["text", "reasoning", "text", "reasoning"]
    # every opened block is closed before the finish
    assert [c["type"] for c in chunks][-3:] == ["reasoning-end", "finish-step", "finish"]


def test_hidden_tools_are_left_out():
    b, chunks = _run(["a", ToolCall(id="c", name="t"), ToolResult(id="c", name="t"), "b"],
                     show_tools=False)
    assert not [c for c in chunks if c["type"].startswith("tool-")]
    assert b.snapshot() == [{"type": "text", "text": "ab"}]


def test_run_error_is_an_error_chunk_not_text():
    b, chunks = _run(["partial", Notice(kind="error", text="RuntimeError: boom",
                                        legacy="\n\n[agent error: RuntimeError: boom]")],
                     status="error")
    assert {"type": "error", "errorText": "RuntimeError: boom"} in chunks
    assert chunks[-1] == {"type": "finish", "messageMetadata": {"status": "error"}}
    assert [c["type"] for c in chunks].count("error") == 1
    assert b.error == "RuntimeError: boom" and b.plain_text() == "partial"


def test_unfinished_tool_calls_are_closed_by_the_outcome():
    b, chunks = _run([ToolCall(id="c1", name="slow_tool")], status="cancelled")
    assert {"type": "tool-output-error", "toolCallId": "c1",
            "errorText": "Stopped before this step finished."} in chunks
    assert b.snapshot()[0]["result"]["status"] == "error"
    b, chunks = _run([ToolCall(id="c1", name="t")], status="complete")
    assert {"type": "tool-output-available", "toolCallId": "c1", "output": {"status": "ok"}} in chunks


def test_failure_outside_the_stream_is_reported_once():
    b = MessageBuilder("m_reply0001", "c_conv00001")
    chunks = b.start() + b.feed("x") + b.fail("RuntimeError: exploded") + b.finish("error")
    assert [c["type"] for c in chunks].count("error") == 1
    assert b.feed("more") == [] and b.finish("complete") == []


def test_result_without_matching_id_closes_the_latest_call_of_that_tool():
    b, chunks = _run([ToolCall(id="", name="a"), ToolCall(id="", name="b"),
                      ToolResult(id="", name="a", ok=True), ToolResult(id="zz", name="b", ok=False)])
    calls = [p for p in b.snapshot() if p["type"] == "tool-call"]
    assert [c["toolCallId"] for c in calls] == ["c1", "c2"]
    assert [c["result"]["status"] for c in calls] == ["ok", "error"]


def test_duplicate_runtime_ids_get_unique_stream_ids():
    b, _ = _run([ToolCall(id="x", name="a"), ToolResult(id="x", name="a"),
                 ToolCall(id="x", name="a"), ToolResult(id="x", name="a")])
    ids = [p["toolCallId"] for p in b.snapshot()]
    assert len(set(ids)) == 2


def test_tool_input_is_a_small_object():
    assert tool_input(None) == {}
    assert tool_input('{"a": 1}') == {"a": 1}
    assert tool_input("not json") == {"input": "not json"}
    assert tool_input([1, 2]) == {"input": [1, 2]}
    big = tool_input({"content": "x" * 10_000})
    assert len(big["content"]) == 2001 and big["content"].endswith("…")
    huge = tool_input({f"k{i}": "y" * 1500 for i in range(50)})
    assert set(huge) == {"preview"}
    assert tool_input({"when": object()})["when"].startswith("<object")
