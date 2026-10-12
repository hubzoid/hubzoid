"""Staff sign-in for the storefront's internal pages."""
from functools import wraps

from flask import abort, g


def login_required(view):
    """Allow only signed-in staff. Every /orders and /admin route uses it."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("staff") is None:
            abort(401)
        return view(*args, **kwargs)

    return wrapped
