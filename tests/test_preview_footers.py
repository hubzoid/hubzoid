"""Footer wording tests for the upload-preview functions.

The point of these tests is wording, not slicing — the previews already
have slice tests in test_read_upload.py. Here we lock in the words that
keep the agent from panicking and escaping to Bash/Read/subagent:

  * No `[truncated]` (reads as "tool failed; find another way").
  * Exact byte counts (so the model knows how much is included).
  * Explicit next-call signature mentioning offset (so paginating feels
    like the obvious next step, not a fallback).
"""
from __future__ import annotations

import json

from hubzoid import upload_previews


def test_text_preview_footer_avoids_truncated_word():
    body = ("\n".join(f"line {i}" for i in range(1, 801))).encode("utf-8")
    out = upload_previews.text_preview(body, offset=1, limit=200)
    assert "[truncated]" not in out
    assert "truncated" not in out.lower()


def test_text_preview_footer_includes_byte_count_and_offset_signature():
    body = ("\n".join(f"line {i}" for i in range(1, 801))).encode("utf-8")
    out = upload_previews.text_preview(body, offset=1, limit=200)
    # Bytes are stated so the model can reason about how much it has.
    assert str(len(body)) in out
    # The next-call shape is explicit, not just hinted.
    assert "read_upload(" in out
    assert "offset=201" in out


def test_json_preview_head_truncation_avoids_truncated_word():
    # Build a big JSON that produces > 50 pretty-printed lines.
    body = json.dumps({"items": [{"id": i, "name": f"x{i}"} for i in range(200)]}).encode()
    out = upload_previews.json_preview(body)
    assert "[truncated]" not in out
    assert "truncated" not in out.lower()


def test_json_preview_head_truncation_mentions_read_upload_full():
    body = json.dumps({"items": [{"id": i, "name": f"x{i}"} for i in range(200)]}).encode()
    out = upload_previews.json_preview(body)
    # If the head is cut, the model should know the escalate tool by name.
    assert "read_upload_full" in out


def test_read_full_ceiling_marker_avoids_truncated_word_and_states_counts(tmp_path, monkeypatch):
    """The ceiling marker on read_upload_full must read as bounded, not broken."""
    from hubzoid import _request_ctx
    from hubzoid import memory as memlib
    from hubzoid import uploads as uploads_lib
    from hubzoid.tools import files as files_mod
    import asyncio
    from agents.tool_context import ToolContext
    from dataclasses import dataclass, field
    from pathlib import Path
    from typing import Any

    @dataclass
    class _Ctx:
        hub_dir: Path
        output_dir: Path
        session_id: str = "s"
        settings: Any = None
        skills: list = field(default_factory=list)
        knowledge: list = field(default_factory=list)

    hub = tmp_path / "hub"
    hub.mkdir()
    out_dir = hub / "output" / "s"
    out_dir.mkdir(parents=True)
    ctx = _Ctx(hub_dir=hub, output_dir=out_dir)

    body = ("x" * (upload_previews.READ_FULL_MAX_CHARS + 50_000)).encode()
    upload_dir = memlib.chat_upload_dir(hub, "c")
    uploads_lib.write_with_meta(upload_dir, "huge.txt", body, mime="text/plain")

    tool = next(t for t in files_mod.make(ctx) if t.name == "read_upload_full")
    _request_ctx.set_chat_id("c")
    try:
        result = asyncio.run(
            tool.on_invoke_tool(
                ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments="{}"),
                json.dumps({"filename": "huge.txt"}),
            )
        )
    finally:
        _request_ctx.set_chat_id(None)

    assert "[truncated]" not in result
    assert "truncated" not in result.lower()
    # Exact totals appear in the marker so the agent can stop guessing.
    assert str(len(body)) in result
    assert str(upload_previews.READ_FULL_MAX_CHARS) in result
