"""PLACEHOLDER tools for the supplier-slip-check hub.

Three typed functions. Two read data (gate slips, goods receipt ledger) and
one posts to the team chat. All return sample data so the hub runs on day
one. Your team replaces the body of each function with the real call (gate
system export, ERP goods receipt table, Slack or Teams incoming webhook) and
keeps the signature, docstring, and return shape.

Files in tools_local/ are auto-discovered at boot.
"""
from __future__ import annotations

import json

from agents import function_tool

_SLIPS = {
    "2026-09-01": [
        {"slip_number": "SLP-77101", "supplier": "Arcadia Packaging", "material": "Corrugated boxes 18x12x10", "quantity": 1500, "unit": "pcs", "material_class": "counted"},
        {"slip_number": "SLP-77102", "supplier": "Arcadia Packaging", "material": "Corrugated boxes 24x18x12", "quantity": 3000, "unit": "pcs", "material_class": "counted"},
        {"slip_number": "SLP-77110", "supplier": "Sundaram Wire Works", "material": "MS wire coil 2.5mm", "quantity": 480, "unit": "kg", "material_class": "weighed"},
        {"slip_number": "SLP-77111", "supplier": "Sundaram Wire Works", "material": "MS wire coil 4.0mm", "quantity": 1020, "unit": "kg", "material_class": "weighed"},
        {"slip_number": "SLP-77115", "supplier": "Arcadia Packaging", "material": "Stretch film 23 micron", "quantity": 6000, "unit": "m", "material_class": "measured"},
    ],
}

_LEDGER = {
    "2026-09-01": [
        {"grn": "GRN-5517", "slip_number": "SLP-77101", "supplier": "Arcadia Packaging", "material": "Corrugated boxes 18x12x10", "quantity": 1500, "unit": "pcs", "date": "2026-09-01"},
        {"grn": "GRN-5518", "slip_number": "SLP-77102", "supplier": "Arcadia Packaging", "material": "Corrugated boxes 24x18x12", "quantity": 2880, "unit": "pcs", "date": "2026-09-01"},
        {"grn": "GRN-5519", "slip_number": "SLP-77111", "supplier": "Sundaram Wire Works", "material": "MS wire coil 4.0mm", "quantity": 1017, "unit": "kg", "date": "2026-09-01"},
        {"grn": "GRN-5520", "slip_number": "SLP-77115", "supplier": "Arcadia Packaging", "material": "Stretch film 23 micron", "quantity": 5960, "unit": "m", "date": "2026-09-02"},
        {"grn": "GRN-5521", "slip_number": "", "supplier": "Kestrel Logistics", "material": "Freight consignments", "quantity": 2, "unit": "pcs", "date": "2026-09-01"},
    ],
}


@function_tool
def supplier_slips(date: str) -> str:
    """Delivery slips received at the gate on one date. PLACEHOLDER sample data.

    Args:
        date: ISO date, e.g. "2026-09-01". Sample data exists for
            2026-09-01 only; other dates return an empty list.

    Returns:
        JSON list of slips with slip_number, supplier, material, quantity,
        unit, and material_class (counted, weighed, or measured).
    """
    return json.dumps(_SLIPS.get(date.strip(), []))


@function_tool
def ledger_receipts(date: str) -> str:
    """Goods receipt entries recorded in the ledger for one date and the next day. PLACEHOLDER sample data.

    Args:
        date: ISO date, e.g. "2026-09-01". Entries dated that day or the
            following day are returned, matching the one-day window in the
            reconciliation rules.

    Returns:
        JSON list of entries with grn, slip_number (empty when the entry was
        recorded without a slip), supplier, material, quantity, unit, date.
    """
    return json.dumps(_LEDGER.get(date.strip(), []))


@function_tool
def post_to_team_chat(message: str) -> str:
    """Post a message to the operations team channel. PLACEHOLDER: records the message, sends nothing.

    Replace the body with a call to your chat platform's incoming webhook
    (Slack, Microsoft Teams, Google Chat) before going live. Keep the
    signature.

    Args:
        message: Plain text, already formatted per the escalation rules.

    Returns:
        A one-line confirmation with the character count.
    """
    return f"PLACEHOLDER post_to_team_chat: recorded {len(message)} characters, nothing was sent."
