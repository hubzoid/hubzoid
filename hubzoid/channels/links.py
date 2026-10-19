"""Account links: which hub account a provider account acts as.

An administrator links a provider account (its workspace and immutable user
id, never a display name or an email address in a message) to a hub account.
A person with no link, or whose account is not active, never reaches a model.
This is the stand-in until run identity has its own store, which adopts these
rows.
"""
from __future__ import annotations

from pathlib import Path

from . import store
from .config import Connection, hub_of
from .errors import ChannelError


def workspace_of(conn: Connection, workspace_id: str | None) -> str:
    if workspace_id:
        return workspace_id
    default = getattr(conn.adapter, "workspace", None)
    found = default(conn) if default else None
    if not found:
        raise ChannelError("validation_failed", "Name the provider workspace (--workspace) for this link.",
                           status=422)
    return found


def link(conn: Connection, *, external_id: str, account: str, actor: str, workspace_id: str | None = None,
         display: str = "") -> dict:
    from ..auth import users

    external_id = (external_id or "").strip()
    if not external_id or len(external_id) > 255:
        raise ChannelError("validation_failed", "external_id is the provider's user id.", status=422)
    user = users.find_by_email(conn.hub_dir, (account or "").strip().lower())
    if user is None:
        raise ChannelError("unknown_account", f"There is no Hubzoid account {account!r}. Add the person first.",
                           status=404)
    row = store.add_link(conn.hub_dir, hub=conn.hub, provider=conn.provider,
                         workspace_id=workspace_of(conn, workspace_id), external_id=external_id,
                         account_id=user["id"], account_email=user["email"].lower(), display=display or "",
                         actor=actor)
    from .config import audit

    audit(conn.hub_dir, actor, "channel_link", f"{conn.provider}:{external_id}->{user['email']}")
    return view(row)


def unlink(hub_dir, lid: str, *, actor: str) -> None:
    if not store.revoke_link(Path(hub_dir), hub_of(hub_dir), lid):
        raise ChannelError("not_found", "No active link with that ID.", status=404)
    from .config import audit

    audit(hub_dir, actor, "channel_unlink", lid)


def links_for(conn: Connection) -> list[dict]:
    return [view(r) for r in store.list_links(conn.hub_dir, conn.hub, conn.provider)]


def view(row: dict) -> dict:
    return {"id": row["id"], "provider": row["provider"], "workspace_id": row["workspace_id"],
            "external_id": row["external_id"], "account": row["account_email"], "display": row["display"],
            "created_by": row["created_by"], "created": row["created"]}
