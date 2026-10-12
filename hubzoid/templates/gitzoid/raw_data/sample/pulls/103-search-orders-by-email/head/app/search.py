"""Order search for the customer care team."""
from flask import Blueprint, jsonify, request

from .auth import login_required
from .db import get_db

bp = Blueprint("search", __name__, url_prefix="/orders/search")


@bp.get("")
@login_required
def search_orders():
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify(error="type at least 3 characters"), 400
    rows = get_db().execute(
        f"SELECT id, email, total, status FROM orders WHERE email LIKE '%{q}%' LIMIT 50"
    ).fetchall()
    return jsonify([dict(r) for r in rows])
