"""Access for tests: every agent is managed in the Console, so a caller needs a
grant, and a bridge call needs a verified person (anonymous calls are refused)."""
from __future__ import annotations

from pathlib import Path

TESTER = "tester@example.org"


def allow(hub_dir, *emails: str, permissions=("use_hub",)) -> None:
    """Grant each person `permissions` in this hub (its key is the folder name)."""
    from hubzoid.access import store_for

    for email in emails or (TESTER,):
        for permission in permissions:
            store_for(hub_dir).grant(email, Path(hub_dir).name, permission, actor="test")


def caller(hub_dir, email: str = TESTER, *, key: str = "dev", surface: str = "api",
           grant: bool = True) -> dict[str, str]:
    """Headers for a bridge call by `email`: the bridge key and the identity
    headers the hub's mode trusts (signed in the web app mode)."""
    from hubzoid import assertions

    if grant:
        allow(hub_dir, email)
    return {"Authorization": f"Bearer {key}",
            **assertions.identity_headers(hub_dir, surface=surface, email=email)}
