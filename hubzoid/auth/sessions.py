"""Session resolution. STUB (lane A replaces the internals, keeps the signatures).

Until lane A lands, local mode resolves to the local owner and sign-in mode
resolves to nobody, so other lanes can build and test against local mode.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Request

from .. import appmode
from . import LOCAL_OWNER_EMAIL, AuthUser

SESSION_COOKIE = "hz_session"


def local_owner(hub_dir: Path) -> AuthUser:
    return AuthUser(id="local-owner", email=LOCAL_OWNER_EMAIL, name="Local owner",
                    role="admin", method="local")


def resolve(request: Request, hub_dir: Path) -> AuthUser | None:
    if not appmode.auth_enabled(hub_dir):
        return local_owner(hub_dir)
    return None
