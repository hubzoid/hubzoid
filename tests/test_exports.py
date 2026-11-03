"""make_excel, make_pdf and make_word: real files, safe input, clear limits."""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from agents.tool_context import ToolContext
from fastapi.testclient import TestClient

from hubzoid import _request_ctx
from hubzoid import memory as memlib
from hubzoid.tools import exports


def _call(tool, **kwargs: Any) -> str:
    args = json.dumps(kwargs)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args)
    return asyncio.run(tool.on_invoke_tool(ctx, args))


@dataclass
class _Ctx:
    hub_dir: Path
    output_dir: Path


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    monkeypatch.delenv("HUBZOID_PUBLIC_URL", raising=False)
    hub = tmp_path / "hub"
    out = hub / "output" / "s"
    out.mkdir(parents=True)
    _request_ctx.set_chat_id(None)
    yield {t.name: t for t in exports.make(_Ctx(hub, out))}, hub
    _request_ctx.set_chat_id(None)


def _artifact(hub: Path, chat: str, name: str) -> Path:
    return memlib.chat_artifact_dir(hub, chat) / name


def test_excel_keeps_types_and_never_makes_formulas(tools):
    from openpyxl import load_workbook

    t, hub = tools
    sheets = [{"name": "Orders", "rows": [["Order", "Qty", "Paid", "Date", "Note"],
                                           ["00123", 3, True, {"date": "2026-11-02"}, "=1+1"],
                                           ["A-2", 2.5, False, None, "=HYPERLINK(\"http://x\")"]]},
              {"name": "Customers", "rows": [["Name"], ["Zoë"]]}]
    with _request_ctx.chat_scope("c1"):
        msg = _call(t["make_excel"], filename="orders", sheets=sheets)
    assert "Download orders.xlsx" in msg and "/artifacts/c1/orders.xlsx?t=" in msg
    wb = load_workbook(_artifact(hub, "c1", "orders.xlsx"))
    assert wb.sheetnames == ["Orders", "Customers"]
    ws = wb["Orders"]
    assert ws["A2"].value == "00123" and ws["B2"].value == 3 and ws["C2"].value is True
    assert ws["D2"].value.date() == dt.date(2026, 11, 2)
    assert ws["E2"].value == "=1+1" and ws["E2"].data_type == "s"
    assert ws["E3"].data_type == "s"
    assert ws["A1"].font.bold
    assert wb["Customers"]["A2"].value == "Zoë"


@pytest.mark.parametrize("sheets, why", [
    ([], "non-empty"),
    ([{"name": "a/b", "rows": []}], "Sheet name"),
    ([{"name": "A", "rows": []}, {"name": "a", "rows": []}], "twice"),
    ([{"name": "A", "rows": [[{"formula": "=1"}]]}], "a cell is"),
    ([{"name": "A", "rows": [[{"date": "02/11/2026"}]]}], "YYYY-MM-DD"),
    ([{"name": "A", "rows": [["x" * 32_768]]}], "longer than"),
])
def test_excel_bad_input_is_a_clear_error_and_writes_nothing(tools, sheets, why):
    t, hub = tools
    with _request_ctx.chat_scope("c2"):
        msg = _call(t["make_excel"], filename="bad.xlsx", sheets=sheets)
    assert "refused" in msg and why in msg and "Nothing was saved" in msg
    assert not _artifact(hub, "c2", "bad.xlsx").exists()


def test_excel_row_limit_checked_before_building(tools, monkeypatch):
    monkeypatch.setattr(exports, "MAX_ROWS", 2)
    t, hub = tools
    with _request_ctx.chat_scope("c3"):
        msg = _call(t["make_excel"], filename="big.xlsx", sheets=[{"name": "A", "rows": [[1], [2], [3]]}])
    assert "limit is 2" in msg and not _artifact(hub, "c3", "big.xlsx").exists()


DOC = """# Weekly summary

Orders are **up** and returns are *down*. <script>alert(1)</script> & co.

![logo](file:///etc/passwd) and [site](http://169.254.169.254/latest)

- First point
- Second point

1. Step one
2. Step two

| Item | Qty | Note |
|---|---|---|
| Widget | 3 | café |
| Gadget | 5 | **bold** |
"""


def test_pdf_has_the_content_and_reads_nothing_external(tools):
    from pypdf import PdfReader

    t, hub = tools
    with _request_ctx.chat_scope("c4"):
        msg = _call(t["make_pdf"], filename="summary.pdf", markdown=DOC, title="Weekly")
    assert "Download summary.pdf" in msg
    reader = PdfReader(_artifact(hub, "c4", "summary.pdf"))
    text = " ".join(p.extract_text() for p in reader.pages)
    for want in ("Weekly summary", "First point", "Step two", "Widget", "café", "<script>alert(1)</script>",
                 "file:///etc/passwd"):
        assert want in text
    assert "**" not in text.replace("**bold**", "")
    assert reader.metadata.title == "Weekly"


def test_pdf_refuses_text_its_font_cannot_draw_and_word_takes_it(tools):
    t, hub = tools
    with _request_ctx.chat_scope("c8"):
        msg = _call(t["make_pdf"], filename="hi.pdf", markdown="# Привет\n\n| a |\n|---|\n| नमस्ते |")
        assert "cannot show" in msg and "make_word" in msg
        assert not _artifact(hub, "c8", "hi.pdf").exists()
        assert "Download hi.docx" in _call(t["make_word"], filename="hi.docx", markdown="# Привет नमस्ते")


def test_word_has_headings_lists_and_a_table(tools):
    from docx import Document

    t, hub = tools
    with _request_ctx.chat_scope("c5"):
        msg = _call(t["make_word"], filename="summary", markdown=DOC)
    assert "Download summary.docx" in msg
    doc = Document(str(_artifact(hub, "c5", "summary.docx")))
    styles = [(p.style.name, p.text) for p in doc.paragraphs]
    assert ("Heading 1", "Weekly summary") in styles
    assert ("List Bullet", "First point") in styles and ("List Number", "Step two") in styles
    assert any("<script>" in text for _, text in styles)
    bold = [r.text for p in doc.paragraphs for r in p.runs if r.bold]
    assert "up" in bold
    table = doc.tables[0]
    assert [c.text for c in table.rows[0].cells] == ["Item", "Qty", "Note"]
    assert table.rows[1].cells[2].text == "café"
    assert not doc.inline_shapes


def test_empty_document_is_refused(tools):
    t, hub = tools
    with _request_ctx.chat_scope("c6"):
        assert "refused" in _call(t["make_pdf"], filename="x.pdf", markdown="  ")
        assert "refused" in _call(t["make_word"], filename="x.docx", markdown="")


def test_filename_cannot_escape_and_gets_the_right_extension(tools):
    t, hub = tools
    with _request_ctx.chat_scope("c7"):
        _call(t["make_pdf"], filename="../../etc/report", markdown="hi")
    assert _artifact(hub, "c7", "report.pdf").is_file()
    assert not (hub / "etc").exists()


def test_without_a_chat_the_file_goes_to_the_session_output(tools):
    t, hub = tools
    msg = _call(t["make_word"], filename="notes.docx", markdown="hello")
    assert (hub / "output" / "s" / "notes.docx").is_file() and "Saved **notes.docx**" in msg


def test_tools_are_built_in_classified_and_not_on_mcp():
    from hubzoid import mcp_server, tool_classes

    for name in ("make_excel", "make_pdf", "make_word"):
        assert tool_classes.BUILTIN[name] == "personal_store"
        assert name in mcp_server.CHAT_SCOPED_TOOLS


def test_optional_title_is_not_required():
    for t in exports.make(_Ctx(Path("/tmp"), Path("/tmp"))):
        assert t.strict_json_schema is False
        assert "title" not in t.params_json_schema["required"]


# ---------------------------------------------------------------------------
# Download route: office files and PDFs are real downloads.
# ---------------------------------------------------------------------------
MINIMAL = Path(__file__).parent / "fixtures" / "minimal_hub"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(MINIMAL))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("BRIDGE_API_KEYS", "test-bridge-key")
    from hubzoid.server import build_app
    yield TestClient(build_app())
    shutil.rmtree(MINIMAL / ".hubzoid", ignore_errors=True)


def test_download_route_serves_exports_as_attachments(client):
    folder = memlib.chat_artifact_dir(MINIMAL, "dl-chat")
    (folder / "Résumé.pdf").write_bytes(exports.to_pdf("# Hi"))
    (folder / "page.html").write_text("<p>x</p>", encoding="utf-8")
    auth = {"Authorization": "Bearer test-bridge-key"}
    r = client.get("/artifacts/dl-chat/R%C3%A9sum%C3%A9.pdf", headers=auth)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert r.headers["content-type"] == "application/pdf"
    disp = r.headers["content-disposition"]
    assert disp.startswith("attachment;") and "filename*=UTF-8''R%C3%A9sum%C3%A9.pdf" in disp
    assert r.headers["x-content-type-options"] == "nosniff"
    page = client.get("/artifacts/dl-chat/page.html", headers=auth)
    assert page.headers["content-disposition"].startswith("inline;")


# ---------------------------------------------------------------------------
# Review fixes (Codex, 2 Nov 2026).
# ---------------------------------------------------------------------------
def test_ragged_table_cannot_blow_up():
    md = "| a |\n|---|\n" + "| x |\n" * 1000 + "|" + " c |" * 1000
    with pytest.raises(exports.ExportError, match="limit is"):
        exports.blocks(md)


def test_table_rule_check_is_linear():
    import time

    start = time.perf_counter()
    exports.blocks("a|b\n" + " " * 200_000 + "x")
    assert time.perf_counter() - start < 1


def test_block_limit_counts_headings(monkeypatch):
    monkeypatch.setattr(exports, "MAX_BLOCKS", 10)
    with pytest.raises(exports.ExportError, match="blocks"):
        exports.blocks("# H\n" * 11)


def test_error_looking_text_stays_text():
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(exports.to_xlsx([{"name": "A", "rows": [["#N/A", "#REF!", "=1"]]}])))
    assert [c.data_type for c in wb["A"][1]] == ["s", "s", "s"]
    assert [c.value for c in wb["A"][1]] == ["#N/A", "#REF!", "=1"]


def test_excel_total_text_is_bounded(monkeypatch):
    monkeypatch.setattr(exports, "MAX_INPUT_CHARS", 10)
    with pytest.raises(exports.ExportError, match="characters of text"):
        exports.to_xlsx([{"name": "A", "rows": [["abcdef", "ghijkl"]]}])


def test_tall_table_cell_splits_across_pages():
    assert exports.to_pdf("| a |\n|---|\n| " + "x " * 5000 + " |").startswith(b"%PDF")


@pytest.mark.parametrize("bad", ["a\x00b", "a\x0bb"])
def test_control_characters_are_a_clear_error(bad):
    with pytest.raises(exports.ExportError, match="control character"):
        exports.to_xlsx([{"name": "A", "rows": [[bad]]}])
    with pytest.raises(exports.ExportError, match="control character"):
        exports.to_docx(bad)


def test_word_table_is_fast():
    import time

    md = "| " + " | ".join(f"c{i}" for i in range(9)) + " |\n|" + "---|" * 9 + "\n"
    md += ("| " + " | ".join("v" for _ in range(9)) + " |\n") * 300
    start = time.perf_counter()
    exports.to_docx(md)
    assert time.perf_counter() - start < 3


def test_save_never_writes_through_a_planted_link(tools, tmp_path):
    t, hub = tools
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    folder = memlib.chat_artifact_dir(hub, "c9")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".report.pdf.part").symlink_to(outside)
    with _request_ctx.chat_scope("c9"):
        assert "Download report.pdf" in _call(t["make_pdf"], filename="report.pdf", markdown="hi")
    assert outside.read_text() == "keep"
    assert not [p for p in folder.iterdir() if p.name.startswith(".export-")]
