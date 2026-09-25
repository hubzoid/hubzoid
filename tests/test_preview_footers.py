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


