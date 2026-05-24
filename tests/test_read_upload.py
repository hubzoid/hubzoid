"""Type-aware `read_upload` tool tests.

The tool is the agent's primary read path for files the user attaches.
Default behavior is bounded — small files come through whole, larger
ones are previewed (head + summary + footer telling the agent how to
fetch more) so a 5 MB JSON doesn't burn 50k tokens on every call.
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


def _put(ctx: _Ctx, chat_id: str, filename: str, payload: bytes, mime: str) -> Path:
    upload_dir = memlib.chat_upload_dir(ctx.hub_dir, chat_id)
    uploads_lib.write_with_meta(upload_dir, filename, payload, mime=mime)
    return upload_dir / filename


def _read(ctx: _Ctx, chat_id: str, **kwargs):
    read = _by_name(files_mod.make(ctx), "read_upload")
    with _request_ctx.chat_scope(chat_id):
        return _call(read, **kwargs)


# ---------------------------------------------------------------------------
# Small-text passthrough (preserves the v0 contract)
# ---------------------------------------------------------------------------
def test_small_text_file_returns_whole_content(ctx):
    _put(ctx, "c", "notes.md", b"hello upload", mime="text/plain")
    out = _read(ctx, "c", filename="notes.md")
    # Header + body. The body is verbatim for small files.
    assert "hello upload" in out
    assert "Path on disk:" in out


# ---------------------------------------------------------------------------
# Large text: default reads first 200 lines + truncation footer
# ---------------------------------------------------------------------------
def test_large_text_file_returns_head_and_footer(ctx):
    body = "\n".join(f"line {i}" for i in range(1, 801)).encode("utf-8")
    _put(ctx, "c", "big.txt", body, mime="text/plain")
    out = _read(ctx, "c", filename="big.txt")
    # Path header is first, then the body lines.
    assert "Path on disk:" in out
    assert "line 1\n" in out
    assert "line 200" in out
    assert "line 201" not in out
    # Footer states total + how to get the next page (no scary words).
    assert "800 total lines" in out
    assert "offset=201" in out
    assert "[truncated]" not in out


def test_text_offset_and_limit(ctx):
    body = "\n".join(f"line {i}" for i in range(1, 801)).encode("utf-8")
    _put(ctx, "c", "big.txt", body, mime="text/plain")
    out = _read(ctx, "c", filename="big.txt", offset=300, limit=5)
    # Lines 300..304 inclusive, in order.
    body_lines = [ln for ln in out.splitlines() if ln.startswith("line ")]
    assert body_lines[:5] == ["line 300", "line 301", "line 302", "line 303", "line 304"]
    assert "800 total lines" in out
    assert "offset=305" in out


def test_text_offset_past_end_returns_helpful_note(ctx):
    body = "\n".join(f"line {i}" for i in range(1, 11)).encode("utf-8")
    _put(ctx, "c", "small.txt", body, mime="text/plain")
    out = _read(ctx, "c", filename="small.txt", offset=999, limit=10)
    assert "of 10" in out or "no lines" in out.lower() or "past end" in out.lower()


# ---------------------------------------------------------------------------
# JSON kind: structural summary on head
# ---------------------------------------------------------------------------
def test_json_object_returns_structural_summary(ctx):
    body = json.dumps({"name": "alice", "items": [1, 2, 3], "meta": {"k": "v"}}).encode()
    _put(ctx, "c", "doc.json", body, mime="application/json")
    out = _read(ctx, "c", filename="doc.json")
    # The summary names top-level keys.
    assert "name" in out and "items" in out and "meta" in out


def test_json_array_summary_mentions_length(ctx):
    body = json.dumps([{"id": i} for i in range(50)]).encode()
    _put(ctx, "c", "arr.json", body, mime="application/json")
    out = _read(ctx, "c", filename="arr.json")
    # Array length + structure of the first element are surfaced.
    assert "50" in out
    assert "id" in out


# ---------------------------------------------------------------------------
# CSV kind: header + sample rows + total count
# ---------------------------------------------------------------------------
def test_csv_returns_header_and_row_count(ctx):
    header = "id,name,score"
    rows = [f"{i},name{i},{i * 10}" for i in range(1, 101)]
    body = ("\n".join([header, *rows]) + "\n").encode()
    _put(ctx, "c", "data.csv", body, mime="text/csv")
    out = _read(ctx, "c", filename="data.csv")
    assert "id,name,score" in out
    # First 20 data rows previewed.
    assert "1,name1,10" in out
    assert "20,name20,200" in out
    # Row 50 is past the 20-row preview and should not appear.
    assert "50,name50,500" not in out
    # Total count surfaced.
    assert "100" in out


# ---------------------------------------------------------------------------
# Image kind: metadata only (no payload bytes)
# ---------------------------------------------------------------------------
def test_image_returns_metadata_only(ctx):
    # 8-byte PNG signature + a few bytes — not a valid PNG, but mime says image.
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    _put(ctx, "c", "pic.png", payload, mime="image/png")
    out = _read(ctx, "c", filename="pic.png")
    assert "image" in out.lower()
    assert "image/png" in out
    # The raw payload bytes do NOT appear in the output.
    assert "\x89PNG" not in out


# ---------------------------------------------------------------------------
# Binary kind: hex preview + size
# ---------------------------------------------------------------------------
def test_binary_returns_hex_preview_and_size(ctx):
    payload = b"\x00\x01\x02\xff\xfe\xfd" + b"\x42" * 100
    _put(ctx, "c", "blob.bin", payload, mime="application/octet-stream")
    out = _read(ctx, "c", filename="blob.bin")
    # The hex preview shows the first bytes.
    assert "00" in out and "ff" in out
    assert "binary" in out.lower()
    # Size is mentioned.
    assert str(len(payload)) in out


# ---------------------------------------------------------------------------
# PDF kind: text extraction with pages param
# ---------------------------------------------------------------------------
def _make_pdf(pages_text: list[str]) -> bytes:
    """Build a minimal multi-page PDF with the given text on each page."""
    from pypdf import PdfWriter
    import io
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
    except ImportError:  # pragma: no cover - dependency may be absent
        pytest.skip("reportlab not installed; cannot build a fixture PDF")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for text in pages_text:
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def test_pdf_extracts_text_default_first_pages(ctx):
    payload = _make_pdf(["page one body", "page two body", "page three body"])
    _put(ctx, "c", "doc.pdf", payload, mime="application/pdf")
    out = _read(ctx, "c", filename="doc.pdf")
    assert "page one body" in out
    # Default pages cap is 5 — all three of ours appear.
    assert "page two body" in out and "page three body" in out


def test_pdf_pages_param_selects_range(ctx):
    payload = _make_pdf([f"page {i} body" for i in range(1, 11)])
    _put(ctx, "c", "doc.pdf", payload, mime="application/pdf")
    out = _read(ctx, "c", filename="doc.pdf", pages="3-4")
    assert "page 3 body" in out and "page 4 body" in out
    assert "page 1 body" not in out and "page 5 body" not in out


# ---------------------------------------------------------------------------
# Sidecar absent (legacy files): falls back to on-the-fly classification
# ---------------------------------------------------------------------------
def test_read_upload_without_sidecar_still_works(ctx):
    # Write the file directly, no sidecar.
    upload_dir = memlib.chat_upload_dir(ctx.hub_dir, "legacy")
    (upload_dir / "old.txt").write_text("legacy content")
    out = _read(ctx, "legacy", filename="old.txt")
    assert "legacy content" in out


# ---------------------------------------------------------------------------
# Listings hide sidecars
# ---------------------------------------------------------------------------
def test_missing_listing_excludes_sidecar_files(ctx):
    _put(ctx, "c", "have.txt", b"x", mime="text/plain")
    out = _read(ctx, "c", filename="missing.txt")
    assert "have.txt" in out
    # The .hubzoid.json sidecar is internal — it should not appear in
    # the directory listing the model sees.
    assert ".hubzoid.json" not in out


# ---------------------------------------------------------------------------
# Absolute path in preview header (so the agent can pass it to other tools
# like extract_for_review(path) or test_template(path))
# ---------------------------------------------------------------------------
def _assert_path_in_preview(out: str, expected_filename: str):
    """The on-disk absolute path of the upload must appear in the preview
    so the agent can chain to tools that take a path argument."""
    assert "Path on disk:" in out, (
        f"preview missing 'Path on disk:' header; got: {out[:200]!r}"
    )
    assert expected_filename in out


def test_text_preview_includes_absolute_path(ctx):
    _put(ctx, "c", "notes.md", b"hello upload", mime="text/plain")
    out = _read(ctx, "c", filename="notes.md")
    _assert_path_in_preview(out, "notes.md")


def test_large_text_preview_includes_absolute_path(ctx):
    body = "\n".join(f"line {i}" for i in range(1, 801)).encode("utf-8")
    _put(ctx, "c", "big.txt", body, mime="text/plain")
    out = _read(ctx, "c", filename="big.txt")
    _assert_path_in_preview(out, "big.txt")


def test_json_preview_includes_absolute_path(ctx):
    body = json.dumps({"k": "v"}).encode()
    _put(ctx, "c", "doc.json", body, mime="application/json")
    out = _read(ctx, "c", filename="doc.json")
    _assert_path_in_preview(out, "doc.json")


def test_csv_preview_includes_absolute_path(ctx):
    body = b"a,b\n1,2\n3,4\n"
    _put(ctx, "c", "data.csv", body, mime="text/csv")
    out = _read(ctx, "c", filename="data.csv")
    _assert_path_in_preview(out, "data.csv")


def test_pdf_preview_includes_absolute_path(ctx):
    payload = _make_pdf(["page one", "page two"])
    _put(ctx, "c", "doc.pdf", payload, mime="application/pdf")
    out = _read(ctx, "c", filename="doc.pdf")
    _assert_path_in_preview(out, "doc.pdf")
