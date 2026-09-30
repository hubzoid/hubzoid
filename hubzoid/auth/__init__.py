"""Hubzoid accounts, sign-in and sessions (the default web app mode).

Public interface used across the codebase. Keep these signatures stable:

    AuthUser                         who is signed in
    current_user(request, hub_dir)   AuthUser or None (never raises for "not signed in")
    require_user(request, hub_dir)   AuthUser or HTTPException(401, code=unauthenticated)
    require_admin(request, hub_dir)  AuthUser with role admin or HTTPException(403)
    local_owner(hub_dir)             the implicit owner when sign-in is off

In local mode (sign-in off, see ``hubzoid.appmode.auth_enabled``) every request
is the local owner, ``admin@localhost``, the same account 1.0.x used, so
workflow ``run_as`` defaults and owner grants carry over.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request

LOCAL_OWNER_EMAIL = "admin@localhost"


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str
    name: str = ""
    role: str = "user"  # 'admin' | 'user'
    method: str = ""    # how this session signed in: password | google | oidc | link | local

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.name or self.email,
                "role": self.role}


def local_owner(hub_dir: Path) -> AuthUser:
    from . import sessions

    return sessions.local_owner(Path(hub_dir))


def current_user(request: Request, hub_dir: Path) -> AuthUser | None:
    from . import sessions

    return sessions.resolve(request, Path(hub_dir))


def require_user(request: Request, hub_dir: Path) -> AuthUser:
    user = current_user(request, hub_dir)
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "unauthenticated",
                                                     "message": "Sign in to continue."})
    return user


def require_admin(request: Request, hub_dir: Path) -> AuthUser:
    user = require_user(request, hub_dir)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail={"code": "forbidden",
                                                     "message": "Administrators only."})
    return user
