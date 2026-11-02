"""Office file exports: Excel, PDF and Word files an agent can hand to the user.

  * `make_excel` — typed rows to an .xlsx (openpyxl).
  * `make_pdf`   — a small Markdown subset to a .pdf (reportlab).
  * `make_word`  — the same Markdown subset to a .docx (python-docx).

Each tool builds the bytes, writes them into the chat's artifacts folder (the
same place `write_artifact` uses) and records the download link. The post-run
scan then versions the file like any other artifact.

Safety rules:
  * No formulas. A string starting with "=" is stored as text, so data the
    agent copies from a customer never becomes spreadsheet code.
  * Documents take plain Markdown blocks only (headings, paragraphs, lists,
    tables, bold, italic). Text is escaped. No images, raw HTML or links, so
    building a document never reads a file or the network.
  * The PDF font (reportlab's bundled Vera) covers Latin text. Text it cannot
    draw is refused with a pointer to make_word instead of printing boxes.
  * Limits are checked before anything is built. Over a limit, the tool
    returns a clear error and writes nothing.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import os
import tempfile
import json
import logging
import re
from pathlib import Path

from agents import FunctionTool

from .. import _request_ctx
from .. import tool_classes
from .files import _artifact_dir, _artifact_url, _format_size, _safe_filename

log = logging.getLogger(__name__)

MAX_INPUT_CHARS = 2_000_000
MAX_SHEETS = 50
MAX_ROWS = 100_000
MAX_COLUMNS = 500
MAX_CELLS = 1_000_000
MAX_TABLE_CELLS = 20_000          # one Markdown table, after padding short rows
MAX_CELL_CHARS = 32_767          # Excel's own cell limit
MAX_BLOCKS = 20_000
MAX_OUTPUT_BYTES = 26_214_400    # the chat file store's default max_file_bytes

_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")
# Control characters XML (and so xlsx and docx) cannot hold. Tab, newline and
# carriage return are fine.
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _check_text(text: str, where: str) -> None:
    if m := _XML_BAD.search(text):
        raise ExportError(f"{where} contains the control character U+{ord(m.group()):04X}. Remove it and retry.")


class ExportError(ValueError):
    """Bad input the model can fix and retry."""


# ---------------------------------------------------------------------------
# Excel.
# ---------------------------------------------------------------------------
def to_xlsx(sheets: list) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    if not isinstance(sheets, list) or not sheets:
        raise ExportError("sheets must be a non-empty list of {name, rows}.")
    if len(sheets) > MAX_SHEETS:
        raise ExportError(f"At most {MAX_SHEETS} sheets.")
    cells = 0
    chars = 0
    names: set[str] = set()
    for i, sheet in enumerate(sheets):
        if not isinstance(sheet, dict) or not isinstance(sheet.get("rows"), list):
            raise ExportError(f"Sheet {i + 1} needs a 'rows' list.")
        name = str(sheet.get("name") or f"Sheet{i + 1}").strip()
        if not name or len(name) > 31 or _SHEET_BAD.search(name):
            raise ExportError(f"Sheet name {name!r}: 1 to 31 characters, none of [ ] : * ? / \\.")
        _check_text(name, "A sheet name")
        if name.lower() in names:
            raise ExportError(f"Sheet name {name!r} is used twice.")
        names.add(name.lower())
        rows = sheet["rows"]
        if len(rows) > MAX_ROWS:
            raise ExportError(f"Sheet {name!r} has {len(rows)} rows; the limit is {MAX_ROWS}.")
        for r, row in enumerate(rows):
            if not isinstance(row, list):
                raise ExportError(f"Sheet {name!r}, row {r + 1}: a row is a list of cells.")
            if len(row) > MAX_COLUMNS:
                raise ExportError(f"Sheet {name!r}, row {r + 1}: more than {MAX_COLUMNS} columns.")
            cells += len(row)
            for value in row:
                _cell_value(value, name, r)   # validate before building
                if isinstance(value, str):
                    chars += len(value)
            if cells > MAX_CELLS:
                raise ExportError(f"More than {MAX_CELLS} cells.")
            if chars > MAX_INPUT_CHARS:
                raise ExportError(f"More than {MAX_INPUT_CHARS} characters of text in all.")

    wb = Workbook()
    wb.remove(wb.active)
    bold = Font(bold=True)
    for i, sheet in enumerate(sheets):
        ws = wb.create_sheet(str(sheet.get("name") or f"Sheet{i + 1}").strip())
        widths: dict[int, int] = {}
        for r, row in enumerate(sheet["rows"], start=1):
            for c, raw in enumerate(row, start=1):
                value = _cell_value(raw, ws.title, r - 1)
                cell = ws.cell(row=r, column=c, value=value)
                if isinstance(value, str):
                    cell.data_type = "s"     # text, never a formula or an error value like #N/A
                if isinstance(value, dt.date):
                    cell.number_format = "yyyy-mm-dd"
                if r == 1:
                    cell.font = bold
                shown = 10 if isinstance(value, dt.date) else len(str(value)) if value is not None else 0
                widths[c] = max(widths.get(c, 0), min(shown, 60))
        for c, w in widths.items():
            ws.column_dimensions[get_column_letter(c)].width = max(8, w + 2)
        if sheet["rows"]:
            ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _cell_value(value, sheet: str, r: int):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            raise ExportError(f"Sheet {sheet!r}, row {r + 1}: numbers must be finite.")
        return value
    if isinstance(value, str):
        if len(value) > MAX_CELL_CHARS:
            raise ExportError(f"Sheet {sheet!r}, row {r + 1}: a cell is longer than {MAX_CELL_CHARS} characters.")
        _check_text(value, f"Sheet {sheet!r}, row {r + 1}")
        return value
    if isinstance(value, dict) and set(value) == {"date"}:
        try:
            return dt.date.fromisoformat(str(value["date"]))
        except ValueError:
            raise ExportError(f"Sheet {sheet!r}, row {r + 1}: dates are {{\"date\": \"YYYY-MM-DD\"}}.") from None
    raise ExportError(f"Sheet {sheet!r}, row {r + 1}: a cell is text, a number, true/false, null "
                      "or {\"date\": \"YYYY-MM-DD\"}.")


# ---------------------------------------------------------------------------
# Markdown blocks, shared by PDF and Word.
# ---------------------------------------------------------------------------
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_RULE_CELL = re.compile(r":?-{2,}:?")
_INLINE = re.compile(r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|_[^_\s][^_]*_)")


def blocks(markdown: str) -> list[tuple]:
    """Split Markdown into ("h", level, text), ("p", text), ("ul"|"ol", [items])
    and ("table", [rows]) blocks. Anything else is kept as paragraph text."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise ExportError("markdown must be non-empty text.")
    if len(markdown) > MAX_INPUT_CHARS:
        raise ExportError(f"The document is longer than {MAX_INPUT_CHARS} characters.")
    _check_text(markdown, "The document")
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[tuple] = []
    para: list[str] = []

    def flush():
        if para:
            out.append(("p", " ".join(s.strip() for s in para)))
            para.clear()

    i = 0
    while i < len(lines):
        if len(out) > MAX_BLOCKS:
            raise ExportError(f"The document has more than {MAX_BLOCKS} blocks.")
        line = lines[i]
        if not line.strip():
            flush()
            i += 1
            continue
        if m := _HEADING.match(line):
            flush()
            out.append(("h", len(m.group(1)), m.group(2).strip().rstrip("#").strip()))
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _is_rule(lines[i + 1]):
            flush()
            rows = [_row(line)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_row(lines[i]))
                i += 1
            width = max(len(r) for r in rows)
            if width > MAX_COLUMNS or width * len(rows) > MAX_TABLE_CELLS:
                raise ExportError(f"A table has {len(rows)} rows of up to {width} columns; the limit is "
                                  f"{MAX_TABLE_CELLS} cells and {MAX_COLUMNS} columns per table.")
            out.append(("table", [r + [""] * (width - len(r)) for r in rows]))
            continue
        for kind, pattern in (("ul", _BULLET), ("ol", _NUMBER)):
            if pattern.match(line):
                flush()
                items = []
                while i < len(lines) and (m := pattern.match(lines[i])):
                    items.append(m.group(1).strip())
                    i += 1
                out.append((kind, items))
                break
        else:
            para.append(line)
            i += 1
    flush()
    if len(out) > MAX_BLOCKS:
        raise ExportError(f"The document has more than {MAX_BLOCKS} blocks.")
    return out


def _is_rule(line: str) -> bool:
    """A table's separator row, such as |---|:--:|. Linear time."""
    s = line.strip()
    if "-" not in s:
        return False
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") else s
    return all(_RULE_CELL.fullmatch(c.strip()) for c in s.split("|"))


def _row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def runs(text: str) -> list[tuple[str, bool, bool]]:
    """Inline text as (text, bold, italic) runs. Only ** and * (or __ and _)."""
    out = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if (part.startswith("**") and part.endswith("**")) or (part.startswith("__") and part.endswith("__")):
            out.append((part[2:-2], True, False))
        elif len(part) > 2 and part[0] == part[-1] and part[0] in "*_":
            out.append((part[1:-1], False, True))
        else:
            out.append((part, False, False))
    return out


# ---------------------------------------------------------------------------
# PDF.
# ---------------------------------------------------------------------------
_FONTS_READY = False


def _fonts() -> str:
    """Register reportlab's bundled Vera TTF family (embedded in the PDF, wider
    coverage than the base-14 fonts). Returns the family name."""
    global _FONTS_READY
    if not _FONTS_READY:
        from reportlab.lib.fonts import addMapping
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        for name, file in (("Vera", "Vera.ttf"), ("Vera-Bold", "VeraBd.ttf"),
                           ("Vera-Italic", "VeraIt.ttf"), ("Vera-BoldItalic", "VeraBI.ttf")):
            pdfmetrics.registerFont(TTFont(name, file))
        addMapping("Vera", 0, 0, "Vera")
        addMapping("Vera", 1, 0, "Vera-Bold")
        addMapping("Vera", 0, 1, "Vera-Italic")
        addMapping("Vera", 1, 1, "Vera-BoldItalic")
        _FONTS_READY = True
    return "Vera"


def _missing_glyphs(parsed: list[tuple]) -> str:
    """Characters the PDF font cannot draw (they would print as boxes)."""
    from reportlab.pdfbase import pdfmetrics

    have = pdfmetrics.getFont("Vera").face.charToGlyph
    texts = []
    for block in parsed:
        for part in block[1:]:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, list):
                texts.extend(c for item in part for c in (item if isinstance(item, list) else [item]))
    missing = []
    for ch in "".join(texts):
        if ord(ch) >= 32 and ord(ch) not in have and ch not in missing:
            missing.append(ch)
    return "".join(missing[:12])


def _rl_markup(text: str) -> str:
    """Escaped reportlab paragraph markup: only <b> and <i> are ours."""
    from xml.sax.saxutils import escape

    out = []
    for part, b, i in runs(text):
        s = escape(part)
        if b:
            s = f"<b>{s}</b>"
        if i:
            s = f"<i>{s}</i>"
        out.append(s)
    return "".join(out)


def to_pdf(markdown: str, title: str = "") -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import ListFlowable, ListItem, LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    parsed = blocks(markdown)
    _check_text(title, "The title")
    font = _fonts()
    if missing := _missing_glyphs(parsed):
        raise ExportError(f"The PDF font cannot show these characters: {missing}. "
                          "Use make_word for this text, or leave them out.")
    base = getSampleStyleSheet()
    body = ParagraphStyle("hz-body", parent=base["BodyText"], fontName=font, fontSize=10.5, leading=14)
    cell = ParagraphStyle("hz-cell", parent=body, fontSize=9, leading=11)
    heads = {n: ParagraphStyle(f"hz-h{n}", parent=base[f"Heading{min(n, 6)}"], fontName=font + "-Bold")
             for n in range(1, 7)}

    doc = SimpleDocTemplate(io.BytesIO(), pagesize=A4, title=title, author="Hubzoid",
                            leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm)
    flow = []
    for block in parsed:
        kind = block[0]
        if kind == "h":
            flow.append(Paragraph(_rl_markup(block[2]), heads[block[1]]))
        elif kind == "p":
            flow.append(Paragraph(_rl_markup(block[1]), body))
        elif kind in ("ul", "ol"):
            items = [ListItem(Paragraph(_rl_markup(t), body), leftIndent=12) for t in block[1]]
            flow.append(ListFlowable(items, bulletType="bullet" if kind == "ul" else "1",
                                     bulletFontName=font, bulletFontSize=9, leftIndent=14))
        elif kind == "table":
            rows = block[1]
            data = [[Paragraph(_rl_markup(c), cell) for c in r] for r in rows]
            data[0] = [Paragraph(f"<b>{_rl_markup(c)}</b>", cell) for c in rows[0]]
            width = doc.width / max(1, len(rows[0]))
            table = LongTable(data, colWidths=[width] * len(rows[0]), repeatRows=1, splitInRow=1)
            table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8c8c8")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            flow.append(table)
        flow.append(Spacer(1, 4))
    from reportlab.platypus.doctemplate import LayoutError

    out = io.BytesIO()
    doc.filename = out
    try:
        doc.build(flow)
    except LayoutError:
        raise ExportError("Part of the document does not fit on a page (for example a very long table "
                          "cell). Shorten it or split it and retry.") from None
    return out.getvalue()


# ---------------------------------------------------------------------------
# Word.
# ---------------------------------------------------------------------------
def to_docx(markdown: str, title: str = "") -> bytes:
    from docx import Document

    parsed = blocks(markdown)
    _check_text(title, "The title")
    doc = Document()
    if title:
        doc.core_properties.title = title
    doc.core_properties.author = "Hubzoid"

    def fill(paragraph, text: str, bold_all: bool = False):
        for part, b, i in runs(text):
            run = paragraph.add_run(part)
            run.bold = b or bold_all or None
            run.italic = i or None

    for block in parsed:
        kind = block[0]
        if kind == "h":
            fill(doc.add_heading(level=min(block[1], 9)), block[2])
        elif kind == "p":
            fill(doc.add_paragraph(), block[1])
        elif kind in ("ul", "ol"):
            style = "List Bullet" if kind == "ul" else "List Number"
            for item in block[1]:
                fill(doc.add_paragraph(style=style), item)
        elif kind == "table":
            rows = block[1]
            table = doc.add_table(rows=len(rows), cols=len(rows[0]))
            table.style = "Table Grid"
            for r, (row, doc_row) in enumerate(zip(rows, table.rows)):
                for text, cell in zip(row, doc_row.cells):     # row.cells once per row: linear
                    fill(cell.paragraphs[0], text, bold_all=(r == 0))
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Tools.
# ---------------------------------------------------------------------------
def _save(hub_dir: Path, fallback_dir: Path, filename: str, ext: str, data: bytes) -> str:
    """Write bytes into the chat's artifacts folder and return the message for
    the model, with the download link when a chat is in scope."""
    name = _safe_filename(filename)
    if not name:
        raise ExportError("filename is empty.")
    if not name.lower().endswith(ext):
        name = f"{name}{ext}"
    if len(data) > MAX_OUTPUT_BYTES:
        raise ExportError(f"The file would be {_format_size(len(data))}; the limit is {_format_size(MAX_OUTPUT_BYTES)}.")
    target_dir = _artifact_dir(hub_dir, fallback_dir)
    target = (target_dir / name).resolve()
    if target_dir.resolve() not in target.parents:
        raise ExportError(f"{filename!r} escapes the artifacts folder.")
    target.parent.mkdir(parents=True, exist_ok=True)
    # A fresh, exclusively created temp file in the same folder, then an atomic
    # rename: never a half-written file under the real name, never a write
    # through a link someone left in the folder.
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".export-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    size = _format_size(len(data))
    url = _artifact_url(name, hub_dir)
    if url:
        _request_ctx.record_artifact(name, url)
        return f"Saved **{name}** ({size})\n\n[Download {name}]({url})"
    return f"Saved **{name}** ({size}) at `{target}`"


def _tool(name: str, description: str, schema: dict, build, save) -> FunctionTool:
    async def invoke(_ctx, args_json: str) -> str:
        try:
            args = json.loads(args_json or "{}")
            if not isinstance(args, dict):
                raise ExportError("Arguments are a JSON object.")
            data = await asyncio.to_thread(build, args)     # building can take seconds: off the event loop
            return save(args, data)
        except ExportError as exc:
            return f"[{name} refused: {exc} Nothing was saved. Fix the input and try again.]"
        except Exception:  # noqa: BLE001 - the model gets a short reason, the log the detail
            log.exception("%s failed", name)
            return f"[{name} failed: the file could not be built. Nothing was saved.]"

    tool = FunctionTool(name=name, description=description, params_json_schema=schema,
                        on_invoke_tool=invoke, strict_json_schema=False)
    return tool_classes.declare(tool, "personal_store")


_CELL = {"anyOf": [
    {"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "null"},
    {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
     "required": ["date"], "additionalProperties": False},
]}

_DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "filename": {"type": "string", "description": "Bare filename, e.g. \"summary.pdf\"."},
        "markdown": {"type": "string", "description": "The document as Markdown: # headings, paragraphs, "
                     "- or 1. lists, | tables |, **bold** and *italic*. Images, links and HTML are shown as text."},
        "title": {"type": "string", "description": "Optional document title (file properties)."},
    },
    "required": ["filename", "markdown"],
    "additionalProperties": False,
}


def make(ctx) -> list:
    hub_dir: Path = ctx.hub_dir
    fallback_dir: Path = ctx.output_dir

    def saver(ext: str):
        return lambda args, data: _save(hub_dir, fallback_dir, str(args.get("filename") or ""), ext, data)

    def excel(args):
        return to_xlsx(args.get("sheets"))

    def pdf(args):
        return to_pdf(args.get("markdown"), str(args.get("title") or ""))

    def word(args):
        return to_docx(args.get("markdown"), str(args.get("title") or ""))

    return [
        _tool("make_excel",
              "Save an Excel (.xlsx) file the user can download. The first row of each sheet is the "
              "header (bold). Cells are text, numbers, true/false, null, or {\"date\": \"YYYY-MM-DD\"}. "
              "Formulas are not supported: text starting with = stays text, so compute totals yourself. "
              "Returns a download link to pass to the user.",
              {"type": "object",
               "properties": {
                   "filename": {"type": "string", "description": "Bare filename, e.g. \"orders.xlsx\"."},
                   "sheets": {"type": "array", "description": "One entry per sheet.", "items": {
                       "type": "object",
                       "properties": {"name": {"type": "string", "description": "Sheet name, up to 31 characters."},
                                      "rows": {"type": "array", "items": {"type": "array", "items": _CELL}}},
                       "required": ["name", "rows"], "additionalProperties": False}},
               },
               "required": ["filename", "sheets"], "additionalProperties": False},
              excel, saver(".xlsx")),
        _tool("make_pdf",
              "Save a PDF the user can download, from Markdown. Returns a download link to pass to the user.",
              _DOC_SCHEMA, pdf, saver(".pdf")),
        _tool("make_word",
              "Save a Word (.docx) document the user can download, from Markdown. Returns a download link "
              "to pass to the user.",
              _DOC_SCHEMA, word, saver(".docx")),
    ]
