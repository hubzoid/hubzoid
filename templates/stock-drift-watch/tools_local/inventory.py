"""PLACEHOLDER tools for the stock-drift-watch hub.

Three typed functions over the stock system. Each returns sample data so the
hub runs on day one. Your team replaces the body of each function with a
call into the warehouse management system or ERP and keeps the signature,
docstring, and return shape. Drift is precomputed here as
counted_qty minus system_qty so the agent never has to do arithmetic on
raw rows.

Files in tools_local/ are auto-discovered at boot.
"""
from __future__ import annotations

import json

from agents import function_tool

_LOCATIONS = ["Pune", "Ahmedabad", "Coimbatore"]

_SNAPSHOTS = {
    "pune": [
        {"sku": "FAS-M8-50", "description": "MS hex bolt M8x50, box of 100", "item_class": "B", "system_qty": 420, "counted_qty": 412, "drift_qty": -8, "unit_cost": 310.0, "last_count": "2026-08-25", "last_movement": "2026-08-31"},
        {"sku": "KIT-SS-A12", "description": "Stainless fastener kit A12", "item_class": "A", "system_qty": 36, "counted_qty": 35, "drift_qty": -1, "unit_cost": 2450.0, "last_count": "2026-08-25", "last_movement": "2026-08-29"},
        {"sku": "PKG-BOX-1812", "description": "Corrugated box 18x12x10", "item_class": "C", "system_qty": 5200, "counted_qty": 5200, "drift_qty": 0, "unit_cost": 12.0, "last_count": "2026-08-18", "last_movement": "2026-09-01"},
        {"sku": "BRG-6205", "description": "Ball bearing 6205", "item_class": "A", "system_qty": 140, "counted_qty": 140, "drift_qty": 0, "unit_cost": 185.0, "last_count": "2026-05-12", "last_movement": "2026-03-30"},
    ],
    "ahmedabad": [
        {"sku": "FAS-M8-50", "description": "MS hex bolt M8x50, box of 100", "item_class": "B", "system_qty": 180, "counted_qty": 180, "drift_qty": 0, "unit_cost": 310.0, "last_count": "2026-08-27", "last_movement": "2026-08-30"},
        {"sku": "FAS-M12-80", "description": "MS hex bolt M12x80, box of 50", "item_class": "B", "system_qty": 260, "counted_qty": 241, "drift_qty": -19, "unit_cost": 540.0, "last_count": "2026-08-27", "last_movement": "2026-08-28"},
        {"sku": "STR-PP-12", "description": "PP strapping 12mm, 1000 m roll", "item_class": "C", "system_qty": 90, "counted_qty": 90, "drift_qty": 0, "unit_cost": 620.0, "last_count": "2026-08-27", "last_movement": "2026-04-02"},
    ],
    "coimbatore": [
        {"sku": "FAS-M8-50", "description": "MS hex bolt M8x50, box of 100", "item_class": "B", "system_qty": 310, "counted_qty": 288, "drift_qty": -22, "unit_cost": 310.0, "last_count": "2026-08-29", "last_movement": "2026-08-31"},
        {"sku": "KIT-SS-A12", "description": "Stainless fastener kit A12", "item_class": "A", "system_qty": 12, "counted_qty": 12, "drift_qty": 0, "unit_cost": 2450.0, "last_count": "2026-08-29", "last_movement": "2026-01-14"},
        {"sku": "WIR-MS-25", "description": "MS wire 2.5mm, kg", "item_class": "B", "system_qty": 1450, "counted_qty": 1520, "drift_qty": 70, "unit_cost": 68.0, "last_count": "2026-08-29", "last_movement": "2026-09-01"},
    ],
}

_MOVEMENTS = {
    "FAS-M8-50": [
        {"date": "2026-08-31", "location": "Pune", "type": "issue", "qty": 40, "reference": "SO-88240"},
        {"date": "2026-08-31", "location": "Coimbatore", "type": "issue", "qty": 25, "reference": "SO-88213"},
        {"date": "2026-08-30", "location": "Ahmedabad", "type": "issue", "qty": 12, "reference": "SO-88201"},
        {"date": "2026-08-26", "location": "Pune", "type": "receipt", "qty": 200, "reference": "GRN-5490"},
        {"date": "2026-08-22", "location": "Coimbatore", "type": "transfer_in", "qty": 100, "reference": "TR-311"},
    ],
    "KIT-SS-A12": [
        {"date": "2026-08-29", "location": "Pune", "type": "issue", "qty": 4, "reference": "SO-88190"},
        {"date": "2026-01-14", "location": "Coimbatore", "type": "issue", "qty": 2, "reference": "SO-84011"},
    ],
}


@function_tool
def locations() -> str:
    """List the stock locations. PLACEHOLDER sample data.

    Returns:
        JSON list of location names.
    """
    return json.dumps(_LOCATIONS)


@function_tool
def stock_snapshot(location: str) -> str:
    """Stock at one location, per SKU, with the last count and precomputed drift. PLACEHOLDER sample data.

    Args:
        location: A name from `locations()`. Case-insensitive.

    Returns:
        JSON with as_of, location, currency, and rows: sku, description,
        item_class (A, B, or C), system_qty, counted_qty, drift_qty
        (counted minus system; negative means the count found less than the
        system says), unit_cost, last_count, last_movement.
    """
    rows = _SNAPSHOTS.get(location.strip().lower())
    if rows is None:
        return f"stock_snapshot: unknown location '{location}'. Known locations: {', '.join(_LOCATIONS)}."
    return json.dumps({"as_of": "2026-09-02T05:30:00+05:30", "location": location.strip().title(), "currency": "INR", "rows": rows})


@function_tool
def sku_movement(sku: str, days: int = 90) -> str:
    """Stock movements for one SKU across all locations over the last N days. PLACEHOLDER sample data.

    Args:
        sku: The SKU code, e.g. "FAS-M8-50". Case-insensitive.
        days: Look-back window in days. The sample data ignores it and
            returns everything recorded.

    Returns:
        JSON list of movements with date, location, type (issue, receipt,
        transfer_in, transfer_out), qty, and reference. Empty when nothing
        is recorded.
    """
    return json.dumps({"sku": sku.strip().upper(), "days": days, "movements": _MOVEMENTS.get(sku.strip().upper(), [])})
