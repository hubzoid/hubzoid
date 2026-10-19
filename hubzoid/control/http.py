"""HTTP pieces every control-plane route shares.

Errors are ``{"detail": {"code", "message", ...optional fields}}`` (the shape
``hubzoid.chat.common.error`` already uses), with optional ``fields``,
``retryable``, ``retry_after`` and ``current_revision`` beside the code. Each
response carries ``X-Request-ID``: the caller's own when it sent a sane one,
else a fresh UUID.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request

REQUEST_ID_HEADER = "X-Request-ID"
_SANE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    detail = {"code": code, "message": message}
    detail.update({k: v for k, v in extra.items() if v is not None})
    return HTTPException(status_code=status, detail=detail)


def conflict(current_revision: int | None, what: str = "this") -> HTTPException:
    return error(409, "revision_conflict",
                 f"Someone changed {what} since you loaded it. Reload and apply again.",
                 current_revision=current_revision)


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise error(400, "invalid_request", "The request body is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise error(400, "invalid_request", "The request body must be a JSON object.")
    return data


def idempotency_key(request: Request) -> str | None:
    return request.headers.get("idempotency-key")


def expected_revision(value: Any, *, allow_absent: bool = True) -> int | None:
    """``expected_revision`` from a body: an integer >= 0, or ``"absent"``
    (create only) which maps to None. Missing is invalid."""
    if value == "absent" and allow_absent:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise error(400, "invalid_request",
                    "expected_revision is required: the revision you loaded, or \"absent\" to create.")
    return value


def install_request_ids(app: FastAPI) -> None:
    """Echo or mint ``X-Request-ID`` on every response (once per app)."""
    if getattr(app.state, "hz_request_ids", False):
        return
    app.state.hz_request_ids = True

    @app.middleware("http")
    async def _request_id(request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER) or ""
        rid = incoming if _SANE_ID.match(incoming) else str(uuid.uuid4())
        request.state.request_id = rid
        response = await call_next(request)
        response.headers.setdefault(REQUEST_ID_HEADER, rid)
        return response
