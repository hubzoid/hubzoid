"""Optional agent tools that propose people and access changes.

A proposal only: the change applies after the proposing manager confirms the
exact plan in the Admin Console. Off unless `HUBZOID_MANAGEMENT_TOOLS` is on,
and effective only on an agent whose access is managed in the Console.

The acting person is always the trusted request identity (`current_identity()`),
never a tool argument. The tools run on `owui`, `web`, `api`, `mcp`, `whatsapp`
and `telegram`, and refuse anonymous callers, scheduled work and Slack. They are
hidden (`is_enabled`) from callers who manage nothing and checked again on every
call, because not every runtime honours `is_enabled`. No tool accepts or returns
a password: a proposed account gets its password on the confirmation page.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import re
import time
from pathlib import Path

from agents import function_tool

log = logging.getLogger("hubzoid.tools.access_admin")


def enabled() -> bool:
    from ..settings import truthy

    return truthy(os.environ.get("HUBZOID_MANAGEMENT_TOOLS"))


def confirm_url(path: str) -> str:
    """Absolute Console link when the public URL is known. A gateway bridge's
    HUBZOID_PUBLIC_URL ends in its artifact prefix (`/b/<hub>`); the Console is
    at the site root."""
    base = (os.environ.get("HUBZOID_PUBLIC_URL") or "").rstrip("/")
    base = re.sub(r"/b/[^/]+$", "", base) or (os.environ.get("WEBUI_URL") or "").rstrip("/")
    return base + path


def make(ctx) -> list:
    if not enabled():
        return []
    hub_dir = Path(ctx.hub_dir)

    from ..access import current_identity, normalize
    from ..access.service import TOOL_SURFACES, AccessService, Actor, Denied

    service = AccessService(hub_dir)
    hub_key = normalize(hub_dir.name)

    def caller() -> tuple[Actor | None, str]:
        """The acting manager, or (None, why not)."""
        ident = current_identity()
        if ident.is_anonymous or not normalize(ident.user or ""):
            return None, "Sign in to manage access."
        if ident.surface not in TOOL_SURFACES:
            return None, f"Access changes can't be proposed from {ident.surface}."
        try:
            if not service.store.is_authoritative(hub_key):
                return None, ("Access for this agent is still managed in the chat app, so "
                              "changes can't be proposed here.")
            actor = Actor(subject=normalize(ident.user), surface=ident.surface, via="bridge")
            if not service.scope(actor).any:
                return None, "You don't manage access to any agent."
        except Denied as exc:
            return None, exc.message
        except Exception:  # noqa: BLE001 — fail closed
            log.exception("access_admin: could not check the caller")
            return None, "Access data is unavailable. Try again shortly."
        return actor, ""

    def is_enabled(*_args, **_kwargs) -> bool:
        return caller()[0] is not None

    def proposed(result: dict, actor: Actor) -> str:
        minutes = max(1, round((result["expires"] - time.time()) / 60))
        link = confirm_url(result["confirm_path"])
        lines = [
            "Proposed, not applied. Nothing changes until you confirm it.",
            f"Change: {result['summary']}",
            f"Review and confirm in the Admin Console within {minutes} minutes: {link}",
        ]
        if actor.surface in ("whatsapp", "telegram"):
            lines.append("Open the link in a browser. Sign in to the chat site first if asked, "
                         "then open the link again.")
        return "\n".join(lines)

    @function_tool
    def my_management_scope() -> str:
        """List the agents whose access you manage and what you can grant in each.

        Read-only. Use this before proposing a change so you only ask for
        capabilities the person asking is allowed to give.
        """
        actor, why = caller()
        if actor is None:
            return f"[not available: {why}]"
        try:
            scope = service.scope(actor)
            grantable = service.grantable(actor)
        except Denied as exc:
            return f"[not available: {exc.message}]"
        lines = ["You are an organization administrator." if scope.org_admin
                 else "You manage access to specific agents."]
        for hub in scope.hubs:
            perms = grantable.get(hub) or []
            lines.append(f"- {hub}: " + (", ".join(perms) if perms else
                                         "access is still managed in the chat app"))
        lines.append("Changes you propose apply only after you confirm them in the Admin Console.")
        return "\n".join(lines)

    @function_tool
    def propose_access_change(person: str, hub: str, grant: list[str] | None = None,
                              revoke: list[str] | None = None) -> str:
        """Propose granting or removing capabilities for one person in one agent.

        Nothing changes until the person asking confirms the exact change in the
        Admin Console. You get a link to give them.

        Args:
            person: The email address of the person whose access changes.
            hub: The agent (hub key) the change applies to.
            grant: Capability names to allow, for example ["use_hub", "crm_read"].
            revoke: Capability names to remove. Removing "use_hub" removes all.
        """
        actor, why = caller()
        if actor is None:
            return f"[not proposed: {why}]"
        try:
            result = service.propose(actor, dict(
                kind="access", hub=hub, subject=person,
                grant=list(grant or []), revoke=list(revoke or [])))
        except Denied as exc:
            return f"[not proposed: {exc.message}]"
        return proposed(result, actor)

    @function_tool
    def propose_new_account(email: str, name: str, hub: str,
                            grant: list[str] | None = None) -> str:
        """Propose creating a chat account for a new person with access to one agent.

        Nothing is created until the person asking confirms in the Admin
        Console, where they also set the password. Never ask for, invent or
        repeat a password.

        Args:
            email: The new person's email address (their sign-in).
            name: The new person's display name.
            hub: The agent (hub key) they should get access to.
            grant: Extra capability names in that agent. Chat access is included.
        """
        actor, why = caller()
        if actor is None:
            return f"[not proposed: {why}]"
        try:
            result = service.propose(actor, dict(
                kind="account", hub=hub, email=email, name=name, grant=list(grant or [])))
        except Denied as exc:
            return f"[not proposed: {exc.message}]"
        return proposed(result, actor)

    return [dataclasses.replace(t, is_enabled=is_enabled)
            for t in (my_management_scope, propose_access_change, propose_new_account)]
