"""What every chat route shares: who is asking, whose conversation it is,
which hub serves it, and the JSON shapes the web app reads.

Errors follow the contract: ``{"detail": {"code": "<snake_case>", "message":
"<sentence for people>"}}``. Someone else's conversation is always 404 (its
existence is never confirmed). A hub-scoped call that reaches the bridge of
another hub is 409 ``wrong_hub``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

from fastapi import HTTPException, Request

from .store import ConversationStore

log = logging.getLogger("hubzoid.chat")

T = TypeVar("T")


def error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def not_found(what: str = "conversation") -> HTTPException:
    return error(404, "not_found", f"That {what} was not found.")


async def db(fn: Callable[..., T], *args, **kwargs) -> T:
    """Run a blocking store call off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def api_bases(hub_dir: Path) -> dict[str, str]:
    """hub key -> the prefix of its hub-scoped calls: '' for a single hub,
    ``/b/<slug>`` in a gateway (from the deployment manifest)."""
    from .. import deployment

    try:
        manifest = deployment.read(Path(hub_dir))
    except Exception:  # noqa: BLE001 — an unreadable manifest means a single hub
        return {}
    out: dict[str, str] = {}
    for entry in manifest.get("hubs") or []:
        key = str(entry.get("key") or "").strip().lower()
        slug = str(entry.get("slug") or "").strip()
        if not slug and entry.get("path"):
            from ..gateway import _slugify

            slug = _slugify(Path(entry["path"]).name)
        if key and slug:
            out[key] = f"/b/{slug}"
    return out


@dataclass
class ChatContext:
    """One bridge's chat service: its hub, runtime and history store."""

    hub_dir: Path
    runtime: Any
    inflight: Any
    settings: Any
    model_label: str
    store: ConversationStore
    runs: Any = None
    hub: str = field(init=False)

    def __post_init__(self) -> None:
        self.hub_dir = Path(self.hub_dir)
        self.hub = self.hub_dir.name.lower()

    # -- who is asking -----------------------------------------------------------
    async def user(self, request: Request):
        from .. import auth

        return await asyncio.to_thread(auth.require_user, request, self.hub_dir)

    @staticmethod
    def same_origin(request: Request) -> None:
        """The cookie is ambient, so a mutation must come from our own origin
        (``access.session.require_same_origin``)."""
        from ..access import session

        try:
            session.require_same_origin(request)
        except HTTPException as exc:
            if isinstance(exc.detail, dict):
                raise
            raise error(exc.status_code, "cross_origin",
                        "This request did not come from this site. Reload the page and try "
                        "again.") from exc

    async def mutation(self, request: Request):
        """A signed-in person's state-changing request from our own origin."""
        self.same_origin(request)
        return await self.user(request)

    async def owned(self, user, conv_id: str) -> dict:
        conv = await db(self.store.owned_conversation, conv_id, user.id)
        if conv is None:
            raise not_found()
        return conv

    def this_hub(self, conv: dict) -> None:
        if conv.get("hub") != self.hub:
            raise error(409, "wrong_hub",
                        "This conversation belongs to another agent's hub. Send the request "
                        "to that hub's address.")

    async def may_use_hub(self, user) -> None:
        await asyncio.to_thread(check_use_hub, self.hub_dir, user.email)

    # -- shapes ------------------------------------------------------------------
    def conversation_json(self, conv: dict, bases: dict[str, str] | None = None) -> dict:
        bases = api_bases(self.hub_dir) if bases is None else bases
        return {
            "id": conv["id"],
            "title": conv.get("title"),
            "title_source": conv.get("title_source"),
            "agent": conv.get("agent"),
            "hub": conv.get("hub"),
            "api_base": bases.get(conv.get("hub") or "", ""),
            "archived": bool(conv.get("archived")),
            "head_id": conv.get("head_id"),
            "created_at": conv.get("created_at"),
            "updated_at": conv.get("updated_at"),
        }


def message_json(message: dict) -> dict:
    return {
        "id": message["id"],
        "parent_id": message.get("parent_id"),
        "role": message.get("role"),
        "content": message.get("content") or [],
        "status": message.get("status"),
        "error": message.get("error"),
        "created_at": message.get("created_at"),
    }


def check_use_hub(hub_dir: Path, email: str) -> None:
    """The hub entry gate for the web app: the same decision as the bridge's
    ``server._enforce_use_hub``, for the signed-in person's email. Suspended
    people are refused; once the hub's access is managed in the Console the
    person needs ``use_hub``. A store failure refuses (503)."""
    from ..access import store_for
    from ..access.identity import normalize
    from ..access.store import USE_HUB

    email = normalize(email)
    try:
        gs = store_for(hub_dir)
        authoritative = gs.is_authoritative(hub_dir.name)
        blocked = bool(email) and gs.is_suspended(email)
    except Exception:  # noqa: BLE001 — fail closed
        log.exception("chat: use_hub check unavailable for %s", hub_dir.name)
        raise error(503, "access_unavailable", "Access check unavailable. Try again shortly.")
    if blocked:
        raise error(403, "suspended", "Your agent access is blocked. Contact your administrator.")
    if not authoritative:
        return
    try:
        allowed = bool(email) and gs.can(email, hub_dir.name, USE_HUB)
        manages = (not allowed) and bool(email) and gs.can(email, hub_dir.name, "manage_access")
    except Exception:  # noqa: BLE001 — fail closed
        log.exception("chat: use_hub can() failed for %s", hub_dir.name)
        raise error(503, "access_unavailable", "Access check unavailable. Try again shortly.")
    if not allowed:
        raise error(403, "no_access", (
            f"You do not have access to the '{hub_dir.name}' hub. Signed in as {email}. "
            + ("Open Console → Agents → Access to review your chat permission." if manages else
               "Ask your hub administrator for chat access, then start a new chat.")))
