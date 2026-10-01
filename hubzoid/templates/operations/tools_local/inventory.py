"""stock_check: stock, days of cover and reorder suggestions from the stock export.

Kestrel & Oak is a fictional company and raw_data/inventory.csv is sample data.
To use your own numbers, replace the CSV with an export in the same columns, or
replace `_rows()` with a call to your inventory system and keep the output.

The arithmetic lives here, not in the model: every figure the agent reports
comes from this tool, and the same question gets the same numbers every time.
Files in tools_local/ are discovered when the hub starts.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

from agents import function_tool

CSV_PATH = Path(__file__).resolve().parents[1] / "raw_data" / "inventory.csv"

# The reorder policy in knowledge/reorder-policy.md. Change both together.
SAFETY_DAYS = 7        # stock kept for surprises, in days of sales
COVER_DAYS = 28        # an order covers four weeks of sales after it arrives
OVERSTOCK_DAYS = 120   # more cover than this ties up cash

_RANK = {"out_of_stock": 0, "stockout_risk": 1, "reorder": 2, "overstock": 3, "ok": 4}


def _rows() -> list[dict]:
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _assess(row: dict, delay_days: int) -> dict:
    on_hand = int(row["on_hand"])
    daily = float(row["avg_daily_sales"])
    lead = int(row["lead_time_days"])
    pack = int(row["case_pack"])
    cost = float(row["unit_cost_eur"])
    on_order = int(row["on_order"] or 0)
    arrives = None
    if on_order and (row.get("order_arrives_in_days") or "").strip():
        arrives = max(0, int(row["order_arrives_in_days"]) + delay_days)

    # The earliest more stock can arrive: the open order, else an order placed today.
    next_stock = arrives if arrives is not None else lead
    runs_out = on_hand / daily if daily > 0 else math.inf
    gap = max(0.0, next_stock - runs_out) if daily > 0 else 0.0
    reorder_point = (lead + SAFETY_DAYS) * daily
    cover = round(runs_out, 1) if daily > 0 else None

    if on_hand == 0 and daily > 0:
        status = "out_of_stock"
    elif gap > 0:
        status = "stockout_risk"
    elif on_hand + on_order < reorder_point:
        status = "reorder"
    elif cover is not None and cover > OVERSTOCK_DAYS and not on_order:
        status = "overstock"
    else:
        status = "ok"

    # Order enough for the lead time, four weeks after delivery and the safety
    # days, less what is here and on its way, in whole cases.
    qty = 0
    if status in ("out_of_stock", "stockout_risk", "reorder"):
        need = (lead + COVER_DAYS + SAFETY_DAYS) * daily - on_hand - on_order
        qty = pack * math.ceil(need / pack) if need > 0 else 0

    if status in ("out_of_stock", "stockout_risk") and arrives is not None:
        action = "expedite the open order" + (" and order more" if qty else "")
    elif status in ("out_of_stock", "stockout_risk"):
        action = "order now: stock runs out before a new order can arrive"
    elif status == "reorder":
        action = "order this week"
    elif status == "overstock":
        action = "no order: overstocked"
    else:
        action = "none"

    return {
        "sku": row["sku"],
        "product": row["product"],
        "supplier": row["supplier"],
        "status": status,
        "action": action,
        "on_hand": on_hand,
        "avg_daily_sales": daily,
        "days_of_cover": cover,
        "on_order": on_order,
        "order_arrives_in_days": arrives,
        "lead_time_days": lead,
        "stockout_days_before_next_delivery": round(gap, 1),
        "days_to_spare": round(max(0.0, runs_out - next_stock), 1) if daily > 0 else None,
        "lost_sales_units_estimate": round(gap * daily),
        "suggested_order_qty": qty,
        "case_pack": pack,
        "order_value_eur": round(qty * cost, 2),
    }


@function_tool
def stock_check(product: str = "", supplier: str = "", delay_days: int = 0) -> str:
    """Stock levels, days of cover and reorder suggestions for Kestrel & Oak (fictional sample data).

    Reads raw_data/inventory.csv and applies the reorder policy in
    knowledge/reorder-policy.md. Days are counted from the stock export.

    Args:
        product: Part of a product name or SKU to look at, for example "mug" or
            "KO-2002". Empty for every product.
        supplier: Part of a supplier name, for example "Harbor". Empty for all.
        delay_days: Days to add to the open orders' arrival, to see what a late
            delivery puts at risk. Use it together with `supplier`.

    Returns:
        JSON with `rows` (most urgent first: out_of_stock, stockout_risk,
        reorder, overstock, ok), each with its status, action, days of cover,
        open order, stockout days before the next delivery (or days to spare
        when stock lasts), estimated lost sales and suggested order quantity
        (whole cases); `order_by_supplier` totals for the suggested orders; and
        the policy `assumptions`. The next delivery is the open order, or an
        order placed today when there is none.
    """
    try:
        rows = _rows()
    except FileNotFoundError:
        return "stock_check: raw_data/inventory.csv is missing. Put a stock export there."
    want_product = product.strip().lower()
    want_supplier = supplier.strip().lower()
    picked = [r for r in rows
              if (not want_product or want_product in r["product"].lower()
                  or want_product in r["sku"].lower())
              and (not want_supplier or want_supplier in r["supplier"].lower())]
    if not picked:
        suppliers = sorted({r["supplier"] for r in rows})
        return (f"stock_check: nothing matches product={product!r} supplier={supplier!r}. "
                f"Suppliers: {', '.join(suppliers)}.")

    assessed = [_assess(r, int(delay_days or 0)) for r in picked]
    assessed.sort(key=lambda a: (_RANK[a["status"]], -a["stockout_days_before_next_delivery"],
                                 a["days_of_cover"] if a["days_of_cover"] is not None else math.inf))
    orders: dict[str, dict] = {}
    for a in assessed:
        if a["suggested_order_qty"]:
            o = orders.setdefault(a["supplier"], {"supplier": a["supplier"], "products": 0,
                                                  "units": 0, "order_value_eur": 0.0})
            o["products"] += 1
            o["units"] += a["suggested_order_qty"]
            o["order_value_eur"] = round(o["order_value_eur"] + a["order_value_eur"], 2)
    return json.dumps({
        "source": "raw_data/inventory.csv (fictional sample data)",
        "delay_days_applied": int(delay_days or 0),
        "assumptions": {"safety_days": SAFETY_DAYS, "cover_days_after_delivery": COVER_DAYS,
                        "overstock_above_days_of_cover": OVERSTOCK_DAYS,
                        "orders_round_up_to": "whole cases"},
        "rows": assessed,
        "order_by_supplier": sorted(orders.values(), key=lambda o: -o["order_value_eur"]),
    })
