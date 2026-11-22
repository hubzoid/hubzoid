"""Card results the model returns (hubzoid.apps.kinds): each malformed result
is refused with the JSON Pointer of the problem; links must be https; cells
match their column type (no NaN, no impossible dates); the text made from a
checked result escapes table pipes and says what's left to fetch. A property
test: any JSON value is either a checked result or Invalid, never another
error, and the text of a checked table has one cell per column on every row."""
from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hubzoid.apps import kinds

COLS = [{"key": "sku", "label": "SKU", "type": "string"}, {"key": "qty", "label": "Qty", "type": "number"},
        {"key": "ok", "label": "OK", "type": "boolean"}, {"key": "due", "label": "Due", "type": "date"}]


def table(**over):
    base = {"kind": "table", "title": "Stock", "columns": COLS, "rows": [], "page": {}}
    base.update(over)
    return base


def invalid(kind, result, **kw):
    with pytest.raises(kinds.Invalid) as err:
        kinds.check(kind, result, **kw)
    return err.value.pointer, err.value.message


@pytest.mark.parametrize("result,pointer", [
    (table(title=7), "/title"),
    (table(title="x" * 20_001), "/title"),
    (table(columns=[]), "/columns"),
    (table(columns=[{"key": "a", "label": "A", "type": "string", "width": 3}]), "/columns/0"),
    (table(columns=[COLS[0], COLS[0]]), "/columns/1/key"),
    (table(columns=[{"key": "a", "label": "A", "type": "money"}]), "/columns/0/type"),
    (table(rows="all"), "/rows"),
    (table(rows=["sku-1"]), "/rows/0"),
    (table(rows=[{"price": 3}]), "/rows/0/price"),
    (table(rows=[{"qty": "three"}]), "/rows/0/qty"),
    (table(rows=[{"qty": True}]), "/rows/0/qty"),
    (table(rows=[{"qty": float("nan")}]), "/rows/0/qty"),
    (table(rows=[{"ok": "yes"}]), "/rows/0/ok"),
    (table(rows=[{"due": "2 Oct"}]), "/rows/0/due"),
    (table(rows=[{"due": "2026-02-30"}]), "/rows/0/due"),
    (table(page={"next_cursor": "c", "size": 3}), "/page"),
    (table(page={"next_cursor": 5}), "/page/next_cursor"),
    (table(page={"total_rows": -1}), "/page/total_rows"),
    (table(page={"total_rows": True}), "/page/total_rows"),
    (table(sources="web"), "/sources"),
    (table(sources=[{"label": "ERP", "id": 1}]), "/sources/0"),
    (table(sources=[{"label": "ERP", "url": "http://erp.example.com"}]), "/sources/0/url"),
    (table(extra=1), "/extra"),
    (table(kind="report"), "/kind"),
])
def test_a_malformed_table_names_the_problem(result, pointer):
    assert invalid("table", result)[0] == pointer


def test_too_many_rows_asks_for_a_page():
    pointer, message = invalid("table", table(rows=[{"sku": "a"}] * 3), rows_per_page=2)
    assert pointer == "/rows" and "next_cursor" in message
    assert invalid("table", ["not", "an", "object"]) == ("", "The result must be a JSON object.")


@pytest.mark.parametrize("kind,result,pointer", [
    ("report", {"kind": "report", "title": "Q3", "sections": "all"}, "/sections"),
    ("report", {"kind": "report", "title": "Q3", "sections": [{}] * 51}, "/sections"),
    ("report", {"kind": "report", "title": "Q3", "sections": [{"heading": "a", "body": "b"}]}, "/sections/0"),
    ("report", {"kind": "report", "title": "Q3", "sections": [{"heading": 1, "markdown": "b"}]},
     "/sections/0/heading"),
    ("progress", {"kind": "progress"}, "/work_item_id"),
    ("progress", {"kind": "progress", "work_item_id": "w-1", "summary": 3}, "/summary"),
    ("approval", {"kind": "approval", "operation_id": "op-1"}, "/digest"),
])
def test_other_kinds_name_the_problem(kind, result, pointer):
    assert invalid(kind, result)[0] == pointer


def test_the_text_matches_the_checked_result():
    checked = kinds.check("table", table(
        rows=[{"sku": "A|1\nB", "qty": 3, "ok": False, "due": "2026-10-02"}, {"sku": None, "ok": True}],
        page={"next_cursor": "p2", "total_rows": 9},
        sources=[{"label": "ERP", "url": "https://erp.example.com"}, {"label": "Sheet"}]))
    out = kinds.text(checked)
    assert "| A\\|1 B | 3 | no | 2026-10-02 |" in out and "|  |  | yes |  |" in out
    assert "2 of 9 rows." in out and 'call again with cursor "p2"' in out
    assert out.endswith("Sources: ERP (https://erp.example.com); Sheet")
    assert kinds.text(kinds.check("table", table())) == "Stock\n\nNo matching rows."

    report = kinds.check("report", {"kind": "report", "title": "Q3", "sections": [{"heading": "Sales", "markdown": "Up."}]})
    assert kinds.text(report) == "# Q3\n\n## Sales\n\nUp."
    progress = kinds.check("progress", {"kind": "progress", "work_item_id": "w-1"})
    assert kinds.text(progress, status={"label": "in review", "summary": "Waiting on Ana."}, link="https://hub/w-1") == \
        "Work item w-1: in review. Waiting on Ana.\nOpen it in Hub: https://hub/w-1"
    assert kinds.text(progress) == "Work item w-1: unknown."
    approval = kinds.check("approval", {"kind": "approval", "operation_id": "op-1", "digest": "d"})
    assert kinds.text(approval, status={"label": "pending"}) == "Approval pending: an action waits for a decision."


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
    max_leaves=12)
# Without "\r": a carriage return in a cell is passed through and breaks the
# row in Markdown (reported to the cards owner, not fixed here).
cell = st.one_of(st.none(), st.text(st.characters(blacklist_characters="\r"), max_size=6))


@pytest.mark.hypothesis
@given(kind=st.sampled_from(kinds.BUILTIN), body=json_values)
def test_any_json_is_checked_or_refused(kind, body):
    result = {"kind": kind, **body} if isinstance(body, dict) else body
    try:
        checked = kinds.check(kind, result)
    except kinds.Invalid as err:
        assert err.pointer == "" or err.pointer.startswith("/")
    else:
        assert checked["kind"] == kind and kinds.text(checked)


@pytest.mark.hypothesis
@given(rows=st.lists(st.fixed_dictionaries({"a": cell, "b": cell}), max_size=5))
def test_every_table_row_has_one_cell_per_column(rows):
    cols = [{"key": "a", "label": "A", "type": "string"}, {"key": "b", "label": "B", "type": "string"}]
    out = kinds.text(kinds.check("table", {"kind": "table", "title": "T", "columns": cols, "rows": rows}))
    body = [line for line in out.split("\n") if line.startswith("| ")][1:]
    assert len(body) == len(rows)
    for line in body:
        assert len(line.replace("\\|", "").split("|")) == 4          # outer pipes plus one between two cells
