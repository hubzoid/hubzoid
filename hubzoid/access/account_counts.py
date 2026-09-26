# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""How many chat accounts a Console viewer is responsible for.

The dashboard's account figure: distinct login accounts in Open WebUI's user
directory, read through the existing read-only connection (`owui_db`). It is
independent of any period, unlike the dashboard's active-users figure.

Scope follows the People list:
  * An organization administrator counts every account in the deployment.
  * A delegate counts only accounts holding a grant in a hub they manage.
    Accounts from other hubs, and their number, are never included.

Counted once however many hubs they hold access in. Blocked accounts still
exist in the directory and are counted; blocking removes every grant, so a
blocked account is outside a delegate's scope. Not counted: service
identities (`workflow:*`), the everyone-signed-in wildcard, and email-only
grant records with no login account.

A directory or access-store failure returns `accounts: None` (unavailable),
never 0, so the Console can say it does not know.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Protocol

from sqlalchemy import text

from . import owui_db
from .identity import normalize
from .store import EVERYONE

log = logging.getLogger("hubzoid.access")


class ViewerScope(Protocol):
    """What the viewer may see: `access.service.Scope` has this shape."""

    org_admin: bool
    hubs: Iterable[str]


def _is_person(subject: str) -> bool:
    return bool(subject) and subject != EVERYONE and not subject.startswith("workflow:")


def _directory_accounts(hub_dir: Path) -> set[str] | None:
    """Normalized emails of every login account, or None when unreadable."""
    con = owui_db.connect_ro(hub_dir)
    if con is None:
        return None
    try:
        try:
            rows = con.execute(text('SELECT email FROM "user"')).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — driver text can hold a URL; never log it
        log.warning("account count: Open WebUI user directory unreadable")
        return None
    return {email for email in (normalize(r[0]) for r in rows) if _is_person(email)}


def _holders(hub_dir: Path, hubs: set[str]) -> set[str] | None:
    """Subjects holding any grant in `hubs`, or None when the store fails."""
    from . import store_for

    try:
        grants = store_for(hub_dir).list_grants()
    except Exception:  # noqa: BLE001
        log.exception("account count: access store unavailable")
        return None
    return {normalize(s) for s, h, _p in grants if normalize(h) in hubs and _is_person(normalize(s))}


def user_account_summary(hub_dir: Path, *, scope: ViewerScope) -> dict:
    """`{"accounts": int | None, "hubs": int}` for this viewer.

    `hubs` is the number of hubs in the viewer's scope (for an organization
    administrator, every hub the deployment registers). `accounts` is None
    when the account directory or, for a delegate, the grants are unreadable.
    """
    hubs = {normalize(h) for h in scope.hubs if normalize(h)}
    result: dict = {"accounts": None, "hubs": len(hubs)}
    accounts = _directory_accounts(Path(hub_dir))
    if accounts is None:
        return result
    if not scope.org_admin:
        holders = _holders(Path(hub_dir), hubs)
        if holders is None:
            return result
        accounts &= holders
    result["accounts"] = len(accounts)
    return result
