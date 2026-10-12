"""Order pages for the customer care team."""
import csv
import io
import logging

from flask import Blueprint, Response, jsonify, request

from .auth import login_required
from .db import get_db

log = logging.getLogger(__name__)
bp = Blueprint("orders", __name__, url_prefix="/orders")


@bp.get("/<int:order_id>")
@login_required
def get_order(order_id: int):
    row = get_db().execute(
        "SELECT id, email, total, status FROM orders WHERE id = ?", (order_id,)
    ).fetchone()
    if row is None:
        return jsonify(error="not found"), 404
    return jsonify(dict(row))


@bp.get("/recent")
@login_required
def recent_orders():
    rows = get_db().execute(
        "SELECT id, total, status FROM orders ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.get("/admin/export")
def export_orders():
    """Every order since a date, with the customer's email and address, as CSV."""
    since = request.args.get("since", "2026-01-01")
    rows = get_db().execute(
        "SELECT id, email, address, total, status, created_at FROM orders WHERE created_at >= ?",
        (since,),
    ).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "email", "address", "total", "status", "created_at"])
    writer.writerows([tuple(r) for r in rows])
    return Response(out.getvalue(), mimetype="text/csv")
