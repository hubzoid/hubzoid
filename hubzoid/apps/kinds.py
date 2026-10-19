"""The built-in card kinds: what each result must look like, and its text.

Every card result is checked here before it is stored, and the model (and
any host that cannot show cards) gets text made from the same checked data,
so the card and the text never disagree. HTML in a string is just text.

| Kind | Result |
|---|---|
| ``table`` | ``{kind, title, columns: [{key, label, type}], rows: [...], page: {next_cursor, total_rows?}, sources: [{label, url?}]}`` |
| ``report`` | ``{kind, title, sections: [{heading, markdown}], sources: [...]}`` |
| ``progress`` | ``{kind, work_item_id, summary?}``: the state is read from the work item |
| ``approval`` | ``{kind, operation_id, digest, summary?}``: an operation waiting for approval |

``question`` and ``plan`` need durable human requests (HUB-36) and plan
digests (HUB-89), which are not built, so they are refused with that reason.
"""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

BUILTIN = ("table", "report", "progress", "approval")
UNAVAILABLE = {
    "question": "The question card needs durable human requests (HUB-36), which are not built yet.",
    "plan": "The plan card needs reviewed plan digests (HUB-89), which are not built yet.",
}
COLUMN_TYPES = ("string", "number", "boolean", "date")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_TEXT = 20_000            # one string in a result
_MAX_SECTIONS = 50
_MAX_COLUMNS = 30


class Invalid(ValueError):
    """The result does not fit its card. ``pointer`` is a JSON Pointer."""

    def __init__(self, message: str, pointer: str = ""):
        super().__init__(message)
        self.message = message
        self.pointer = pointer


def _text(value: Any, pointer: str, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise Invalid("must be text", pointer)
    if len(value) > _MAX_TEXT:
        raise Invalid(f"is longer than {_MAX_TEXT} characters", pointer)
    return value


def _sources(value: Any, pointer: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise Invalid("must be a list", pointer)
    out = []
    for i, s in enumerate(value):
        if not isinstance(s, dict) or set(s) - {"label", "url"}:
            raise Invalid("must be {label, url?}", f"{pointer}/{i}")
        item = {"label": _text(s.get("label"), f"{pointer}/{i}/label")}
        url = s.get("url")
        if url is not None:
            url = _text(url, f"{pointer}/{i}/url")
            if not url.startswith("https://"):
                raise Invalid("must be an https:// link", f"{pointer}/{i}/url")
            item["url"] = url
        out.append(item)
    return out


def _cell(value: Any, kind: str, pointer: str) -> Any:
    if value is None:
        return None
    if kind == "string":
        return _text(value, pointer)
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Invalid("must be a number", pointer)
        if isinstance(value, float) and not math.isfinite(value):
            raise Invalid("must be a finite number", pointer)
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise Invalid("must be true or false", pointer)
        return value
    if not isinstance(value, str) or not _DATE.match(value):
        raise Invalid("must be a calendar date (YYYY-MM-DD)", pointer)
    try:
        date.fromisoformat(value)
    except ValueError:
        raise Invalid("must be a real calendar date", pointer) from None
    return value


def check(kind: str, result: Any, *, rows_per_page: int = 100) -> dict:
    """The checked result of a ``kind`` card. Raises ``Invalid``."""
    if not isinstance(result, dict):
        raise Invalid("The result must be a JSON object.")
    if result.get("kind") != kind:
        raise Invalid(f"must be {kind!r}", "/kind")
    allowed = {"table": {"kind", "title", "columns", "rows", "page", "sources"},
               "report": {"kind", "title", "sections", "sources"},
               "progress": {"kind", "work_item_id", "summary"},
               "approval": {"kind", "operation_id", "digest", "summary"}}[kind]
    extra = sorted(set(result) - allowed)
    if extra:
        raise Invalid(f"unknown field {extra[0]!r}", f"/{extra[0]}")
    if kind == "table":
        return _table(result, rows_per_page)
    if kind == "report":
        sections = result.get("sections")
        if not isinstance(sections, list) or len(sections) > _MAX_SECTIONS:
            raise Invalid(f"must be a list of at most {_MAX_SECTIONS} sections", "/sections")
        out = []
        for i, s in enumerate(sections):
            if not isinstance(s, dict) or set(s) - {"heading", "markdown"}:
                raise Invalid("must be {heading, markdown}", f"/sections/{i}")
            out.append({"heading": _text(s.get("heading"), f"/sections/{i}/heading"),
                        "markdown": _text(s.get("markdown"), f"/sections/{i}/markdown")})
        return {"kind": kind, "title": _text(result.get("title"), "/title"), "sections": out,
                "sources": _sources(result.get("sources"), "/sources")}
    if kind == "progress":
        return {"kind": kind, "work_item_id": _text(result.get("work_item_id"), "/work_item_id"),
                "summary": _text(result.get("summary"), "/summary", required=False)}
    return {"kind": kind, "operation_id": _text(result.get("operation_id"), "/operation_id"),
            "digest": _text(result.get("digest"), "/digest"),
            "summary": _text(result.get("summary"), "/summary", required=False)}


def _table(result: dict, rows_per_page: int) -> dict:
    columns = result.get("columns")
    if not isinstance(columns, list) or not columns or len(columns) > _MAX_COLUMNS:
        raise Invalid(f"must list 1 to {_MAX_COLUMNS} columns", "/columns")
    cols, keys = [], set()
    for i, c in enumerate(columns):
        if not isinstance(c, dict) or set(c) - {"key", "label", "type"}:
            raise Invalid("must be {key, label, type}", f"/columns/{i}")
        key = _text(c.get("key"), f"/columns/{i}/key")
        if not key or key in keys:
            raise Invalid("must be a unique, non-empty key", f"/columns/{i}/key")
        if c.get("type") not in COLUMN_TYPES:
            raise Invalid(f"must be one of {', '.join(COLUMN_TYPES)}", f"/columns/{i}/type")
        keys.add(key)
        cols.append({"key": key, "label": _text(c.get("label"), f"/columns/{i}/label"), "type": c["type"]})
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise Invalid("must be a list", "/rows")
    if len(rows) > rows_per_page:
        raise Invalid(f"has more than {rows_per_page} rows; return a page and a next_cursor", "/rows")
    types = {c["key"]: c["type"] for c in cols}
    out_rows = []
    for r, row in enumerate(rows):
        if not isinstance(row, dict):
            raise Invalid("must be an object", f"/rows/{r}")
        unknown = sorted(set(row) - keys)
        if unknown:
            raise Invalid(f"has a column that is not declared ({unknown[0]!r})", f"/rows/{r}/{unknown[0]}")
        out_rows.append({k: _cell(row.get(k), types[k], f"/rows/{r}/{k}") for k in types})
    page = result.get("page") or {}
    if not isinstance(page, dict) or set(page) - {"next_cursor", "total_rows"}:
        raise Invalid("must be {next_cursor, total_rows?}", "/page")
    cursor = page.get("next_cursor")
    if cursor is not None:
        _text(cursor, "/page/next_cursor")
    out_page: dict = {"next_cursor": cursor}
    if page.get("total_rows") is not None:
        total = page["total_rows"]
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise Invalid("must be a whole number", "/page/total_rows")
        out_page["total_rows"] = total
    return {"kind": "table", "title": _text(result.get("title"), "/title"), "columns": cols,
            "rows": out_rows, "page": out_page, "sources": _sources(result.get("sources"), "/sources")}


def _md_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _sources_text(sources: list[dict]) -> str:
    if not sources:
        return ""
    return "\n\nSources: " + "; ".join(
        f"{s['label']} ({s['url']})" if s.get("url") else s["label"] for s in sources)


def text(result: dict, *, status: dict | None = None, link: str | None = None) -> str:
    """Readable text for a checked result. ``status`` is the authoritative state
    of a progress or approval card; ``link`` where to act on it in Hub."""
    kind = result["kind"]
    if kind == "table":
        lines = [result["title"], ""]
        if not result["rows"]:
            lines.append("No matching rows.")
        else:
            cols = result["columns"]
            lines.append("| " + " | ".join(_md_cell(c["label"]) for c in cols) + " |")
            lines.append("|" + "---|" * len(cols))
            for row in result["rows"]:
                lines.append("| " + " | ".join(_md_cell(row.get(c["key"])) for c in cols) + " |")
        page = result["page"]
        if page.get("total_rows") is not None:
            lines.append(f"\n{len(result['rows'])} of {page['total_rows']} rows.")
        if page.get("next_cursor"):
            lines.append("More rows are available.")
        return "\n".join(lines).rstrip() + _sources_text(result["sources"])
    if kind == "report":
        parts = [f"# {result['title']}"]
        for s in result["sections"]:
            parts.append(f"## {s['heading']}\n\n{s['markdown']}".rstrip())
        return "\n\n".join(parts) + _sources_text(result["sources"])
    state = (status or {}).get("label") or "unknown"
    summary = result.get("summary") or (status or {}).get("summary") or ""
    if kind == "progress":
        out = f"Work item {result['work_item_id']}: {state}."
    else:
        out = f"Approval {state}: {summary or 'an action waits for a decision.'}"
        summary = ""
    if summary:
        out += f" {summary}"
    if link:
        out += f"\nOpen it in Hub: {link}"
    return out
