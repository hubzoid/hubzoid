"""The few ClickUp API v2 calls the channel makes, with the connection's token.

Calls follow Agno's ClickUp toolkit (``libs/agno/agno/tools/clickup.py``):
the token goes in the ``Authorization`` header as it is, against
``https://api.clickup.com/api/v2``. The token never leaves the host: no
ClickUp tool is offered to agents, and nothing here is reachable from a
sandbox.

A write is called refused (``Rejected``) only when ClickUp clearly did not run
it (a 4xx other than 429). A timeout, 429 or 5xx stays uncertain, so the
reply outbox can look before it posts again.
"""
from __future__ import annotations

import os

from ..errors import ChannelError

TIMEOUT = 15


def base_url() -> str:
    return (os.environ.get("HUBZOID_CLICKUP_API_URL") or "https://api.clickup.com/api/v2").rstrip("/")


def task_url(task_id: str) -> str:
    return f"https://app.clickup.com/t/{task_id}"


def call(conn, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
         write: bool = False, token: str | None = None) -> dict:
    """One API call. Returns the JSON body. Raises `Rejected` for a refused
    write, `ChannelError` (503) for a token ClickUp refuses or an outage on a
    read, and `RuntimeError` for an uncertain write."""
    import httpx

    token = token or conn.secret("token_ref")
    try:
        resp = httpx.request(method, f"{base_url()}{path}", json=json, params=params, timeout=TIMEOUT,
                             headers={"Authorization": token, "Content-Type": "application/json"},
                             follow_redirects=False)
    except httpx.HTTPError as exc:
        if write:
            raise RuntimeError(f"ClickUp did not answer: {type(exc).__name__}") from None
        raise ChannelError("provider_unavailable", "ClickUp did not answer.", status=503) from None
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = {}
    if resp.status_code < 400:
        return data if isinstance(data, dict) else {"items": data}
    message = str((data or {}).get("err") or resp.status_code)[:300]
    if resp.status_code == 401 or (data or {}).get("ECODE") in ("OAUTH_019", "OAUTH_025", "OAUTH_027"):
        if write:
            from ...work_items.errors import Rejected

            raise Rejected(f"ClickUp refused the channel's token: {message}")
        raise ChannelError("reauth_required", f"ClickUp refused the channel's token: {message}", status=503)
    if write and 400 <= resp.status_code < 500 and resp.status_code != 429:
        from ...work_items.errors import Rejected

        raise Rejected(f"ClickUp refused the request: {message}")
    if write:
        raise RuntimeError(f"ClickUp error {resp.status_code}: {message}")
    if resp.status_code == 404:
        raise ChannelError("not_found", f"ClickUp has no such item: {message}", status=404)
    if resp.status_code in (400, 403):
        raise ChannelError("permission_denied", f"ClickUp refused: {message}", status=403)
    raise ChannelError("provider_unavailable", f"ClickUp error {resp.status_code}: {message}", status=503)


def get_task(conn, task_id: str) -> dict:
    return call(conn, "GET", f"/task/{task_id}", params={"include_markdown_description": "true"})


def me(conn, *, token: str | None = None) -> dict:
    return (call(conn, "GET", "/user", token=token) or {}).get("user") or {}


def comments(conn, task_id: str) -> list[dict]:
    return list((call(conn, "GET", f"/task/{task_id}/comment") or {}).get("comments") or [])


def post_comment(conn, task_id: str, text: str) -> dict:
    return call(conn, "POST", f"/task/{task_id}/comment", json={"comment_text": text, "notify_all": False},
                write=True)


def set_status(conn, task_id: str, status: str) -> dict:
    return call(conn, "PUT", f"/task/{task_id}", json={"status": status}, write=True)


def get_list(conn, list_id: str) -> dict:
    return call(conn, "GET", f"/list/{list_id}")


def webhooks(conn, workspace_id: str) -> list[dict]:
    return list((call(conn, "GET", f"/team/{workspace_id}/webhook") or {}).get("webhooks") or [])


def create_webhook(conn, workspace_id: str, *, endpoint: str, events: list[str], list_id: str) -> dict:
    return call(conn, "POST", f"/team/{workspace_id}/webhook",
                json={"endpoint": endpoint, "events": events, "list_id": int(list_id)}, write=True)


def delete_webhook(conn, webhook_id: str) -> dict:
    return call(conn, "DELETE", f"/webhook/{webhook_id}", write=True)
