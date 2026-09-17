"""Small OWUI API adapter. Accounts remain owned by OWUI; never write its DB."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import httpx
from dotenv import dotenv_values

from .. import deployment
from ..gateway_provision import _signin_or_bootstrap


@contextmanager
def client_for(hub_dir):
    env = {**dotenv_values(Path(hub_dir) / ".env"), **os.environ}
    base = deployment.owui_url(Path(hub_dir))
    if not base:
        raise RuntimeError("Open WebUI URL is not configured; start the gateway first")
    with httpx.Client(base_url=base, timeout=10) as client:
        email = env.get("HUBZOID_GATEWAY_ADMIN_EMAIL", "")
        password = env.get("HUBZOID_GATEWAY_ADMIN_PASSWORD", "")
        if not email or not password:
            raise RuntimeError(
                "Set HUBZOID_GATEWAY_ADMIN_EMAIL/PASSWORD for account lookup and visibility sync"
            )
        token = _signin_or_bootstrap(client, email, password, False)
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


def users(client) -> list[dict]:
    result = []
    page = 1
    expected_total = None
    seen = set()
    while True:
        r = client.get("/api/v1/users/", params={"page": page})
        r.raise_for_status()
        data = r.json()
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("users"), list)
            or type(data.get("total")) is not int
            or data["total"] < 0
        ):
            raise ValueError("Incomplete OWUI directory response")
        if expected_total is None:
            expected_total = data["total"]
        if expected_total != data["total"]:
            raise ValueError("OWUI directory changed while paging; retry refresh")
        batch = data["users"]
        for user in batch:
            if (
                not isinstance(user, dict)
                or not user.get("id")
                or not user.get("email")
                or user["id"] in seen
            ):
                raise ValueError("Invalid or repeated OWUI directory account")
            seen.add(user["id"])
        result.extend(batch)
        if len(result) == expected_total:
            return result
        if not batch or len(result) > expected_total:
            raise ValueError("Incomplete OWUI directory page; retry refresh")
        page += 1


def directory(hub_dir) -> list[dict]:
    with client_for(hub_dir) as c:
        return [
            dict(
                subject=u["email"].strip().lower(),
                email=u["email"],
                display=u.get("name", ""),
                owui_id=u["id"],
                role=u.get("role", "user"),
            )
            for u in users(c)
        ]
