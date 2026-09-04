"""PLACEHOLDER tools for the accounts-desk hub.

Four typed functions over the purchase ledger. Each returns sample data so the
hub runs on day one. Your team replaces the body of each function with a call
into the real accounting system or ERP and keeps the signature, docstring, and
return shape. The agent reads the docstrings to decide when to call each tool.

Files in tools_local/ are auto-discovered at boot.
"""
from __future__ import annotations

import json
import math

from agents import function_tool

_PURCHASE_ORDERS = {
    "PO-1187": {
        "po_number": "PO-1187",
        "supplier": "Arcadia Packaging",
        "total": 45000,
        "currency": "INR",
        "status": "open",
        "receiving_location": "Pune",
        "lines": [
            {"item": "Corrugated boxes 18x12x10", "qty": 3000, "unit_price": 12.0, "amount": 36000},
            {"item": "Stretch film 23 micron", "qty": 60, "unit_price": 150.0, "amount": 9000},
        ],
    },
    "PO-1190": {
        "po_number": "PO-1190",
        "supplier": "Kestrel Logistics",
        "total": 12400,
        "currency": "INR",
        "status": "open",
        "receiving_location": "Ahmedabad",
        "lines": [
            {"item": "Freight, Pune to Ahmedabad, 2 consignments", "qty": 2, "unit_price": 6200.0, "amount": 12400},
        ],
    },
    "PO-1203": {
        "po_number": "PO-1203",
        "supplier": "Brightline Stationers",
        "total": 3150,
        "currency": "INR",
        "status": "open",
        "receiving_location": "Pune",
        "lines": [
            {"item": "A4 copier paper, 10 reams", "qty": 10, "unit_price": 315.0, "amount": 3150},
        ],
    },
}

_BILLS = [
    {"bill_number": "INV-2291", "supplier": "Arcadia Packaging", "total": 48600, "date": "2026-08-30", "po_number": "PO-1187", "status": "pending"},
    {"bill_number": "INV-2288", "supplier": "Kestrel Logistics", "total": 12400, "date": "2026-08-28", "po_number": "PO-1190", "status": "pending"},
    {"bill_number": "INV-2288", "supplier": "Kestrel Logistics", "total": 12400, "date": "2026-09-01", "po_number": "PO-1190", "status": "pending"},
    {"bill_number": "INV-0771", "supplier": "Brightline Stationers", "total": 3150, "date": "2026-09-01", "po_number": "", "status": "pending"},
    {"bill_number": "INV-2201", "supplier": "Arcadia Packaging", "total": 45000, "date": "2026-07-14", "po_number": "PO-1102", "status": "posted"},
]

_SUPPLIERS = {
    "arcadia packaging": {"supplier": "Arcadia Packaging", "payment_terms_days": 30, "on_hold": False, "category": "Packaging materials"},
    "kestrel logistics": {"supplier": "Kestrel Logistics", "payment_terms_days": 15, "on_hold": False, "category": "Freight and transport inward"},
    "brightline stationers": {"supplier": "Brightline Stationers", "payment_terms_days": 30, "on_hold": True, "hold_reason": "GST registration lapsed, awaiting new certificate", "category": "Office supplies and stationery"},
}


@function_tool
def find_purchase_order(po_number: str) -> str:
    """Look up one purchase order by number. PLACEHOLDER sample data.

    Args:
        po_number: The PO number as printed on the bill, e.g. "PO-1187".

    Returns:
        JSON with supplier, total, currency, status, receiving_location, and
        lines. A short message when the PO does not exist.
    """
    key = po_number.strip().upper()
    po = _PURCHASE_ORDERS.get(key)
    if po is None:
        return f"find_purchase_order: no purchase order '{po_number}' in the ledger."
    return json.dumps(po)


@function_tool
def search_bills(supplier: str, amount: float) -> str:
    """Find bills from a supplier at a given total, for duplicate checks. PLACEHOLDER sample data.

    Args:
        supplier: Supplier name as it appears on the bill. Case-insensitive.
        amount: Bill total. Bills within 1 of this amount are returned.

    Returns:
        JSON list of bills with bill_number, total, date, po_number, and
        status. An empty list means nothing similar is in the ledger.
    """
    wanted = supplier.strip().lower()
    hits = [
        b for b in _BILLS
        if b["supplier"].lower() == wanted and math.isclose(b["total"], amount, abs_tol=1.0)
    ]
    return json.dumps(hits)


@function_tool
def pending_bills() -> str:
    """List supplier bills received but not yet matched or posted. PLACEHOLDER sample data.

    Returns:
        JSON list of bills with bill_number, supplier, total, date,
        po_number (empty when the bill carries none), and status.
    """
    return json.dumps([b for b in _BILLS if b["status"] == "pending"])


@function_tool
def supplier_profile(supplier: str) -> str:
    """Payment terms, purchase category, and hold status for a supplier. PLACEHOLDER sample data.

    Args:
        supplier: Supplier name. Case-insensitive.

    Returns:
        JSON with supplier, payment_terms_days, on_hold, hold_reason when on
        hold, and category (matches the chart-of-accounts purchase types).
    """
    profile = _SUPPLIERS.get(supplier.strip().lower())
    if profile is None:
        return f"supplier_profile: no supplier named '{supplier}' in the ledger."
    return json.dumps(profile)
