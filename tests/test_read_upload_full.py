"""`read_upload_full` — the escalate tool for when the head wasn't enough.

Bypasses the head/limit cap but still enforces a hard character ceiling so
a runaway file can't crush the prompt. Refuses binary kinds outright —
the agent should use a different tool for those.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from agents.tool_context import ToolContext

from hubzoid import _request_ctx
from hubzoid import memory as memlib
from hubzoid import uploads as uploads_lib
from hubzoid.tools import files as files_mod


def _call(tool, **kwargs: Any) -> str:
    args = json.dumps(kwargs)
    ctx = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="test",
        tool_arguments=args,
    )
    return asyncio.run(tool.on_invoke_tool(ctx, args))


def _by_name(tools: list, name: str):
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(f"tool {name!r} not in {[t.name for t in tools]}")


@dataclass
class _Ctx:
    hub_dir: Path
    output_dir: Path
    session_id: str = "test-session"
    settings: Any = None
    skills: list = field(default_factory=list)
    knowledge: list = field(default_factory=list)


@pytest.fixture
def ctx(tmp_path: Path) -> _Ctx:
    hub = tmp_path / "hub"
    hub.mkdir()
    out = hub / "output" / "sess"
    out.mkdir(parents=True)
    return _Ctx(hub_dir=hub, output_dir=out)


@pytest.fixture(autouse=True)
def _reset_request_ctx():
    _request_ctx.set_chat_id(None)
    yield
    _request_ctx.set_chat_id(None)


def _put(ctx: _Ctx, chat_id: str, filename: str, payload: bytes, mime: str):
    upload_dir = memlib.chat_upload_dir(ctx.hub_dir, chat_id)
    uploads_lib.write_with_meta(upload_dir, filename, payload, mime=mime)


def _read_full(ctx: _Ctx, chat_id: str, **kwargs):
    tool = _by_name(files_mod.make(ctx), "read_upload_full")
    with _request_ctx.chat_scope(chat_id):
        return _call(tool, **kwargs)


def test_read_upload_full_returns_entire_small_text(ctx):
    body = "\n".join(f"line {i}" for i in range(1, 51)).encode("utf-8")
    _put(ctx, "c", "small.txt", body, mime="text/plain")
    out = _read_full(ctx, "c", filename="small.txt")
    # All 50 lines present.
    for i in (1, 25, 50):
        assert f"line {i}" in out
    # No "use offset/limit" footer — full means full.
    assert "Showing lines" not in out


def test_read_upload_full_truncates_at_hard_ceiling(ctx):
    """Files over the cap come back with an explicit byte-count footer
    (no scary `[truncated]` word — that triggers escape behavior)."""
    from hubzoid import upload_previews
    body = ("x" * (upload_previews.READ_FULL_MAX_CHARS + 50_000)).encode("utf-8")
    _put(ctx, "c", "huge.txt", body, mime="text/plain")
    out = _read_full(ctx, "c", filename="huge.txt")
    assert "[truncated]" not in out
    assert "truncated" not in out.lower()
    assert str(upload_previews.READ_FULL_MAX_CHARS) in out
    assert str(len(body)) in out
    # Cap + a few hundred chars of footer.
    assert len(out) <= upload_previews.READ_FULL_MAX_CHARS + 500


def test_read_upload_full_refuses_binary(ctx):
    _put(ctx, "c", "blob.bin", b"\x00\x01\x02\xff" + b"x" * 100, mime="application/octet-stream")
    out = _read_full(ctx, "c", filename="blob.bin")
    assert "binary" in out.lower()
    assert "refused" in out.lower() or "cannot" in out.lower()


def test_read_upload_full_returns_entire_json_text(ctx):
    """Unlike read_upload (which summarizes), full returns the raw JSON bytes
    as text so the agent can scan a small-to-medium file end-to-end."""
    body = json.dumps({"a": list(range(100))}).encode()
    _put(ctx, "c", "doc.json", body, mime="application/json")
    out = _read_full(ctx, "c", filename="doc.json")
    # Full content survived (no structural summary header).
    assert '"a":' in out or '"a"' in out
    # And the 99 element is present (would be elided by a summary).
    assert "99" in out


def test_read_upload_full_requires_chat_in_scope(ctx):
    tool = _by_name(files_mod.make(ctx), "read_upload_full")
    out = _call(tool, filename="anything.txt")
    assert "no chat is in scope" in out
