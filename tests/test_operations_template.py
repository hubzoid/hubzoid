"""The default `hubzoid init` template: Kestrel & Oak, a fictional shop's
operations assistant. Its first answers come from the stock_check tool, so the
tool's numbers are pinned here, along with the files the agent is told to use.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from hubzoid import frontmatter
from hubzoid.loaders import agents as agents_loader
from hubzoid.loaders import knowledge as knowledge_loader
from hubzoid.loaders import tools_local

HUB = Path(__file__).resolve().parents[1] / "hubzoid" / "templates" / "operations"


@pytest.fixture(scope="module")
def stock_check():
    return tools_local.load_all(HUB)["stock_check"]


def _call(tool, **args) -> dict:
    from agents.tool_context import ToolContext

    raw = json.dumps(args)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=raw)
    out = asyncio.run(tool.on_invoke_tool(ctx, raw))
    return json.loads(out)


def _rows(result: dict) -> dict[str, dict]:
    return {r["sku"]: r for r in result["rows"]}


def test_reorder_answer_numbers(stock_check):
    result = _call(stock_check)
    rows = _rows(result)
    assert len(rows) == 21
    assert [r["status"] for r in result["rows"][:5]] == ["out_of_stock"] + ["stockout_risk"] * 4
    candle = rows["KO-5001"]  # out of stock: order now, in whole cases of 12
    assert (candle["status"], candle["suggested_order_qty"], candle["order_value_eur"]) == ("out_of_stock", 144, 734.4)
    mugs = rows["KO-1001"]    # the open order arrives after the mugs run out
    assert mugs["status"] == "stockout_risk" and mugs["action"] == "expedite the open order"
    assert (mugs["stockout_days_before_next_delivery"], mugs["suggested_order_qty"]) == (3.2, 0)
    assert rows["KO-1012"]["status"] == "overstock"
    assert rows["KO-2002"]["status"] == "ok" and rows["KO-2002"]["suggested_order_qty"] == 0
    totals = {o["supplier"]: o["order_value_eur"] for o in result["order_by_supplier"]}
    assert totals == {"Linden Textiles": 2816.0, "Fenwick Appliances": 2307.6,
                      "Copperline Cookware": 1339.2, "Harbor Ceramics": 882.0, "Ember and Wick": 734.4}


def test_supplier_delay_answer_numbers(stock_check):
    rows = _rows(_call(stock_check, supplier="harbor", delay_days=5))
    assert set(rows) == {"KO-1001", "KO-1002", "KO-1003", "KO-1010", "KO-1011", "KO-1012"}
    mugs = rows["KO-1001"]
    assert (mugs["order_arrives_in_days"], mugs["stockout_days_before_next_delivery"],
            mugs["lost_sales_units_estimate"]) == (17, 8.2, 77)
    plates = rows["KO-1010"]  # still in time, with little to spare
    assert (plates["status"], plates["days_to_spare"]) == ("ok", 1.5)
    assert rows["KO-1011"]["status"] == "stockout_risk"  # at risk without the delay too


def test_filters_and_unknown_names(stock_check):
    assert list(_rows(_call(stock_check, product="blender"))) == ["KO-2002"]
    assert list(_rows(_call(stock_check, product="ko-2001"))) == ["KO-2001"]
    from agents.tool_context import ToolContext

    raw = json.dumps({"supplier": "nobody"})
    out = asyncio.run(stock_check.on_invoke_tool(
        ToolContext(context=None, tool_name="stock_check", tool_call_id="t", tool_arguments=raw), raw))
    assert out.startswith("stock_check: nothing matches") and "Harbor Ceramics" in out


def test_the_tool_applies_the_written_policy():
    """The tool's constants and knowledge/reorder-policy.md say the same thing."""
    policy = (HUB / "knowledge" / "reorder-policy.md").read_text()
    tool = (HUB / "tools_local" / "inventory.py").read_text()
    for constant, phrase in (("SAFETY_DAYS = 7", "7 days of sales"),
                             ("COVER_DAYS = 28", "28 days of sales"),
                             ("OVERSTOCK_DAYS = 120", "120 days of cover")):
        assert constant in tool and phrase in policy


def test_the_claude_runtime_gets_the_same_tool(stock_check):
    """Runtime neutrality: the Claude adapter returns the tool's own output."""
    from hubzoid.factory_claude import _to_claude_tool

    wrapped = _to_claude_tool(stock_check)
    out = asyncio.run(wrapped.handler({"supplier": "Ember"}))
    assert json.loads(out["content"][0]["text"]) == _call(stock_check, supplier="Ember")


def test_agent_prompts_and_sources_exist():
    main = agents_loader.load_main(HUB)
    assert main.spec.name == "Kestrel & Oak Ops"
    assert len(main.spec.suggestions) == 3
    assert all(len(s) <= 90 for s in main.spec.suggestions)
    body = (HUB / "AGENTS.md").read_text()
    assert "fictional" in body
    names = {k.name for k in knowledge_loader.load_all(HUB)}
    assert names == {"company", "suppliers", "reorder-policy", "returns-policy", "customer-messages"}
    for name in names | {"reorder-plan", "supplier-delay", "stock_check"}:
        assert f"`{name}`" in body, name
    for skill in ("reorder-plan", "supplier-delay"):
        meta, _ = frontmatter.read(HUB / "skills" / f"{skill}.md")
        assert meta["name"] == skill and meta["description"]


def test_everything_is_labelled_fictional_and_keeps_to_house_style():
    assert "fictional" in (HUB / "README.md").read_text()
    assert "fictional" in (HUB / "knowledge" / "company.md").read_text()
    assert "fictional" in (HUB / "raw_data" / "README.md").read_text()
    for path in HUB.rglob("*.md"):
        text = path.read_text()
        assert "—" not in text and "–" not in text, path   # no em or en dashes
    emails = re.findall(r"[\w.+-]+@([\w.-]+)", (HUB / "knowledge" / "suppliers.md").read_text())
    assert emails and all(domain.endswith(".example") for domain in emails)


def test_evals_and_schedule_load():
    from hubzoid import scheduling
    from hubzoid.evals import cases

    found = {c.name: c for c in cases.discover(HUB)}
    assert found["reorder-uses-the-data"].expect_tools == ["stock_check"]
    assert "returns-after-30-days" in found
    tasks, problems = scheduling.load_tasks(HUB)
    assert problems == [] and [(t.name, t.enabled) for t in tasks] == [("monday-reorder", False)]
