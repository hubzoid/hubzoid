"""Order pages for the customer care team."""
import logging

from flask import Blueprint, jsonify

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
