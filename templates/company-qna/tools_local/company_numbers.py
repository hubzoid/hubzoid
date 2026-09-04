"""PLACEHOLDER tools for the company-qna hub.

Two typed functions over the metric store. Both return sample data so the
hub runs on day one. Your team replaces the body of `metric` with a query
against the reporting database or BI layer and keeps the signature,
docstring, and return shape. The names in `list_metrics` must stay in step
with knowledge/metric-definitions.md.

Files in tools_local/ are auto-discovered at boot.
"""
from __future__ import annotations

import json

from agents import function_tool

_STORE = {
    "revenue": {
        "2026-06": {"value": 40800000, "currency": "INR"},
        "2026-07": {"value": 43100000, "currency": "INR"},
        "2026-08": {"value": 41650000, "currency": "INR"},
    },
    "billing_by_customer": {
        "2026-08": {
            "currency": "INR",
            "top_10": [
                {"customer": "Kalpak Auto Components", "amount": 5830000},
                {"customer": "Sundaram Fabricators", "amount": 3750000},
                {"customer": "Brightline Packaging", "amount": 2920000},
                {"customer": "Meher Engineering", "amount": 2110000},
                {"customer": "Ravi Traders", "amount": 1640000},
                {"customer": "Orion Tools", "amount": 1390000},
                {"customer": "Deccan Motors", "amount": 1220000},
                {"customer": "Lakshmi Fabrication", "amount": 980000},
                {"customer": "Vega Industrial", "amount": 870000},
                {"customer": "Anand Precision", "amount": 810000},
            ],
        },
    },
    "gross_margin_pct": {
        "2026-07": {"value": 21.4},
        "2026-08": {"value": 20.6},
    },
    "cash_balance": {
        "2026-07": {"value": 11200000, "currency": "INR"},
        "2026-08": {"value": 9800000, "currency": "INR"},
    },
    "receivables_overdue": {
        "2026-08": {"total": 2140000, "count": 17, "currency": "INR"},
    },
    "headcount": {
        "2026-08": {"full_time": 142, "contract": 31},
    },
    "orders": {
        "2026-07": {"value": 1710},
        "2026-08": {"value": 1655},
    },
}


@function_tool
def list_metrics() -> str:
    """List the metric names in the store and the periods each one covers. PLACEHOLDER sample data.

    Returns:
        JSON object mapping metric name to the list of available periods
        (YYYY-MM).
    """
    return json.dumps({name: sorted(periods) for name, periods in _STORE.items()})


@function_tool
def metric(name: str, period: str) -> str:
    """Fetch one metric for one period from the store. PLACEHOLDER sample data.

    Args:
        name: A metric name from `list_metrics()`, e.g. "revenue".
        period: Calendar month as YYYY-MM, e.g. "2026-08".

    Returns:
        JSON with name, period, and the stored fields for that metric. A
        short message when the metric or the period is not in the store.
    """
    key = name.strip().lower()
    if key not in _STORE:
        return f"metric: no metric named '{name}'. Known metrics: {', '.join(sorted(_STORE))}."
    periods = _STORE[key]
    p = period.strip()
    if p not in periods:
        return f"metric: {key} has no data for '{p}'. Available periods: {', '.join(sorted(periods))}."
    return json.dumps({"name": key, "period": p, **periods[p]})
