"""PLACEHOLDER tools for the morning-briefing hub.

Three typed functions, one per system the briefing reads. Each returns sample
data so the hub runs on day one. Your team replaces the body of each function
with a call into the real system (ERP, accounting package, bank feed, WMS)
and keeps the signature, docstring, and return shape. The agent reads the
docstrings to decide when to call each tool.

Files in tools_local/ are auto-discovered at boot. Files starting with an
underscore are skipped.
"""
from __future__ import annotations

import json

from agents import function_tool

_PERIODS = ("yesterday", "mtd", "qtd")


@function_tool
def sales_snapshot(period: str) -> str:
    """Orders, revenue, and pipeline from the sales system. PLACEHOLDER sample data.

    Args:
        period: One of "yesterday", "mtd" (month to date), or "qtd"
            (quarter to date). Anything else returns an error message.

    Returns:
        JSON with as_of, orders, revenue, largest_orders, plan_to_date,
        and weighted_pipeline. Amounts in the reporting currency.
    """
    period = period.strip().lower()
    if period not in _PERIODS:
        return (
            f"sales_snapshot: unknown period '{period}'. "
            f"Supported periods: {', '.join(_PERIODS)}. Historical periods are not available."
        )
    sample = {
        "yesterday": {
            "as_of": "2026-09-01T23:59:00+05:30",
            "orders": 71,
            "revenue": 1740000,
            "largest_orders": [
                {"customer": "Kalpak Auto Components", "amount": 412000},
                {"customer": "Sundaram Fabricators", "amount": 188000},
                {"customer": "Meher Engineering", "amount": 96000},
            ],
            "plan_to_date": 1750000,
            "weighted_pipeline": 61000000,
        },
        "mtd": {
            "as_of": "2026-09-01T23:59:00+05:30",
            "orders": 71,
            "revenue": 1740000,
            "largest_orders": [
                {"customer": "Kalpak Auto Components", "amount": 412000},
            ],
            "plan_to_date": 1750000,
            "weighted_pipeline": 61000000,
        },
        "qtd": {
            "as_of": "2026-09-01T23:59:00+05:30",
            "orders": 4310,
            "revenue": 84900000,
            "largest_orders": [
                {"customer": "Kalpak Auto Components", "amount": 2100000},
            ],
            "plan_to_date": 85750000,
            "weighted_pipeline": 61000000,
        },
    }
    return json.dumps({"period": period, "currency": "INR", **sample[period]})


@function_tool
def cash_position() -> str:
    """Bank balances, receivables, payables, and runway. PLACEHOLDER sample data.

    Returns:
        JSON with as_of, bank_balance, receivables_due_7d, payables_due_7d,
        overdue_receivables (per customer, with days overdue), and
        runway_weeks. Amounts in the reporting currency.
    """
    data = {
        "as_of": "2026-09-02T06:00:00+05:30",
        "currency": "INR",
        "bank_balance": 9800000,
        "receivables_due_7d": 6400000,
        "payables_due_7d": 7100000,
        "overdue_receivables": [
            {"customer": "Brightline Packaging", "amount": 310000, "days_overdue": 34},
            {"customer": "Meher Engineering", "amount": 84000, "days_overdue": 12},
        ],
        "runway_weeks": 14,
    }
    return json.dumps(data)


@function_tool
def ops_snapshot() -> str:
    """Open orders, late shipments, capacity, and incidents. PLACEHOLDER sample data.

    Returns:
        JSON with as_of, open_orders, late_orders (list with location and days
        late), capacity_used_pct per location, and open_incidents with hours
        open.
    """
    data = {
        "as_of": "2026-09-02T06:00:00+05:30",
        "open_orders": 214,
        "late_orders": [
            {"order": "SO-88213", "customer": "Sundaram Fabricators", "location": "Coimbatore", "days_late": 2},
            {"order": "SO-88240", "customer": "Meher Engineering", "location": "Coimbatore", "days_late": 1},
            {"order": "SO-88251", "customer": "Ravi Traders", "location": "Pune", "days_late": 1},
        ],
        "capacity_used_pct": {"Pune": 88.0, "Ahmedabad": 71.5, "Coimbatore": 94.2},
        "open_incidents": [
            {"id": "INC-104", "summary": "Coimbatore dispatch printer down", "hours_open": 27},
        ],
    }
    return json.dumps(data)
