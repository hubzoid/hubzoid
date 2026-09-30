"""Agent tools that show access and propose people and access changes.

Three read tools (`my_management_scope`, `who_has_access`, `explain_access`)
and two that only propose (`propose_access_change`, `propose_new_account`): a
proposal applies after the proposing manager confirms the exact plan in the
Admin Console.

Who gets them: a person granted "Manage access from chat" (`access_tools`, an
organization-administrator-only grant) in the agent where the chat runs, who
also manages access (organization administrator or hub delegate). What they
see or propose stays bounded by that management scope and ceiling. Effective
only on an agent whose access is managed in the Console.

Switches: `HUBZOID_ACCESS_TOOLS=false` (or `HUBZOID_MANAGEMENT_TOOLS=false`)
removes the tools from the agent. `HUBZOID_MANAGEMENT_TOOLS=true` keeps its
1.0.x meaning for one release: every manager gets the tools without the grant.
It is deprecated and logged once.

The acting person is always the trusted request identity (`current_identity()`),
never a tool argument. The tools run on `owui`, `web`, `api`, `mcp`, `whatsapp`
and `telegram`, and refuse anonymous callers, scheduled work and Slack. They are
hidden (`is_enabled`, and `access.guard.visible` for the runtimes and the MCP
listing that don't evaluate it) from callers who fail these checks, and checked
again on every call. No tool accepts or returns a password: a proposed account
gets its password on the confirmation page.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import re
import time
from pathlib import Path

from agents import function_tool

from ..capabilities import Capability, register, switched_off

log = logging.getLogger("hubzoid.tools.access_admin")

ACCESS_TOOLS = register(Capability(
    permission="access_tools", label="Manage access from chat", group="tools", section="access",
    surfaces=("chat", "mcp"), sensitive=True, delegate_grantable=False,
    description=("See who has access and propose access changes, from chat or an assistant, "
                 "in the agents this person manages. Has an effect only for people who manage "
                 "access. Every change is confirmed in the Console."),
    enabled_by=("HUBZOID_ACCESS_TOOLS", "HUBZOID_MANAGEMENT_TOOLS"),
))

NOT_GRANTED = ("Manage access from chat is not turned on for you in this agent. An "
               "organization administrator can turn it on in the Console.")
MAX_ROWS = 50

_legacy_warned = False


def legacy() -> bool:
    """`HUBZOID_MANAGEMENT_TOOLS=true`: the deprecated 1.0.x switch that gives
    the tools to every manager without the `access_tools` grant."""
    from ..settings import truthy

    return truthy(os.environ.get("HUBZOID_MANAGEMENT_TOOLS"))


def _warn_legacy() -> None:
    global _legacy_warned
    if not _legacy_warned:
        _legacy_warned = True
        log.warning("HUBZOID_MANAGEMENT_TOOLS=true is deprecated: every access manager gets the "
                    "access tools without a grant. Grant \"Manage access from chat\" "
                    "(access_tools) in the Console instead, then remove the setting.")


def confirm_url(path: str) -> str:
    """Absolute Console link when the public URL is known. A gateway bridge's
    HUBZOID_PUBLIC_URL ends in its artifact prefix (`/b/<hub>`); the Console is
    at the site root."""
    base = (os.environ.get("HUBZOID_PUBLIC_URL") or "").rstrip("/")
    base = re.sub(r"/b/[^/]+$", "", base) or (os.environ.get("WEBUI_URL") or "").rstrip("/")
    return base + path


def _who(display: str | None, subject: str) -> str:
    return f"{display} <{subject}>" if display and display != subject else subject


def _row_flags(row: dict) -> list[str]:
    """The account flags of one `AccessService.hub_access` row, in words."""
    flags = []
    if row.get("suspended"):
        flags.append("blocked")
    status = row.get("status")
    if status == "pending-approval":
        flags.append("awaiting approval")
    elif status == "awaiting-signup":
        flags.append("awaiting sign-up")
    if row.get("account_unavailable") and status != "pending-approval":
        flags.append("account unavailable")
    if "manage_access" in (row.get("inherited") or ()):
        flags.append("organization administrator")
    if row.get("kind") == "service":
        flags.append("service identity")
    return flags


def _account(view: dict) -> str:
    """One person's account state (`AccessService.person_access`), in words."""
    status = view.get("status")
    if view.get("suspended"):
        return "blocked by an administrator"
    if status == "pending-approval":
        return "awaiting approval"
    if status == "awaiting-signup":
        return "no chat account yet (awaiting sign-up)"
    if view.get("account_unavailable"):
        return "unavailable in the chat app"
    if status == "service":
        return "service identity"
    return "active"


_SOURCE_WORDS = {"direct": "direct grant", "everyone": "everyone signed in",
                 "organization": "organization administrator"}


def make(ctx) -> list:
    if switched_off(ACCESS_TOOLS):
        return []
    hub_dir = Path(ctx.hub_dir)

    from ..access import current_identity, normalize
    from ..access.guard import decide
    from ..access.service import TOOL_SURFACES, AccessService, Actor, Denied
    from ..access.store import EVERYONE

    service = AccessService(hub_dir)
    hub_key = normalize(hub_dir.name)
    grant_free = legacy()
    if grant_free:
        _warn_legacy()

    def caller() -> tuple[Actor | None, str]:
        """The acting manager, or (None, why not)."""
        ident = current_identity()
        if ident.is_anonymous or not normalize(ident.user or ""):
            return None, "Sign in to manage access."
        if ident.surface not in TOOL_SURFACES:
            return None, f"Access can't be managed from {ident.surface}."
        try:
            if not service.store.is_authoritative(hub_key):
                return None, ("Access for this agent is still managed in the chat app, so "
                              "it can't be managed from here.")
            if not grant_free:
                # The grant in the agent where the chat runs, decided like any
                # guarded tool (surface, block, grant; fail closed).
                allowed, reason = decide(hub_dir, ident, ACCESS_TOOLS.permission, TOOL_SURFACES)
                if not allowed:
                    return None, ("Access data is unavailable. Try again shortly."
                                  if reason == "store-error" else NOT_GRANTED)
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

    # Marked like the access guard's checks, so the Claude and Codex runtimes
    # and the MCP listing (which do not evaluate is_enabled) also leave these
    # tools out for people who fail caller() (access.guard.visible). caller()
    # still gates each call.
    is_enabled.hubzoid_permission = ACCESS_TOOLS.permission

    def names(hub: str) -> dict[str, str]:
        try:
            return {e["permission"]: e["label"] for e in service.catalog(hub)}
        except Denied:
            return {}

    def labelled(hub: str, perms, known: dict[str, str] | None = None) -> str:
        """Console labels with the id the tools take, e.g. 'Save shared knowledge (curator)'."""
        known = names(hub) if known is None else known
        return ", ".join(f"{known[p]} ({p})" if known.get(p) and known[p] != p else p for p in perms)

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
            lines.append(f"- {hub}: " + (labelled(hub, perms) if perms else
                                         "access is still managed in the chat app"))
        lines.append("Use the id in parentheses when proposing a change.")
        lines.append("Changes you propose apply only after you confirm them in the Admin Console.")
        return "\n".join(lines)

    @function_tool
    def who_has_access(hub: str) -> str:
        """List the people and services with access to one agent you manage.

        Read-only. Shows each one's capabilities (Console label and id), account
        flags such as blocked or awaiting approval, and whether everyone signed
        in can use the agent.

        Args:
            hub: The agent (hub key) to list, as my_management_scope names it.
        """
        actor, why = caller()
        if actor is None:
            return f"[not available: {why}]"
        try:
            view = service.hub_access(actor, hub)
        except Denied as exc:
            return f"[not available: {exc.message}]"
        hub = view["hub"]
        known = names(hub)
        rows = [r for r in view["rows"] if r["subject"] != EVERYONE]
        lines = [f"{hub}: {len(rows)} " + ("person or service has" if len(rows) == 1 else
                                          "people and services have") + " access."]
        if not view["authoritative"]:
            lines.append("Access to this agent is still managed in the chat app, so this list "
                         "may not match who can use it.")
        if view["public"]:
            reliant = view["public_reliant"]
            lines.append("Everyone signed in can use this agent" + (
                f"; {reliant} of them {'has' if reliant == 1 else 'have'} no access of their own here."
                if reliant else "."))
        else:
            lines.append("Not open to everyone signed in.")
        for row in rows[:MAX_ROWS]:
            flags = _row_flags(row)
            lines.append(f"- {_who(row['display'], row['subject'])}: "
                         + (labelled(hub, sorted(row["perms"]), known) or "no grants in this agent")
                         + (f" [{', '.join(flags)}]" if flags else ""))
        if len(rows) > MAX_ROWS:
            lines.append(f"... and {len(rows) - MAX_ROWS} more, see the Console.")
        return "\n".join(lines)

    @function_tool
    def explain_access(person: str, hub: str = "") -> str:
        """Explain one person's access in the agents you manage, and why they have it.

        Read-only. For each capability, says where it comes from: a direct
        grant, everyone signed in, or organization administrator. Also gives
        the account state, for example blocked or awaiting approval.

        Args:
            person: The email address (or service id) of the person to explain.
            hub: Optional agent (hub key) to limit the answer to. Empty means
                every agent you manage.
        """
        actor, why = caller()
        if actor is None:
            return f"[not available: {why}]"
        try:
            view = service.person_access(actor, person, hub or None)
        except Denied as exc:
            return f"[not available: {exc.message}]"
        subject = view["subject"]
        held = [h for h in view["hubs"] if h["capabilities"]]
        if not view.get("known", True):
            # Not someone this delegate manages: no account detail, only what
            # everyone signed in gets in the agents they manage.
            public = [h["hub"] for h in held]
            if not public:
                return f"{subject} has no access in the agents you manage."
            return (f"{subject} has no access of their own in the agents you manage. "
                    f"Everyone signed in can use: {', '.join(public)}.")
        if not held:
            # Nothing about other agents or the account: only what this caller manages.
            return (f"{subject} has no access to {view['hubs'][0]['hub']}." if hub and view["hubs"]
                    else f"{subject} has no access in the agents you manage.")
        lines = [f"{_who(view['display'], subject)}. Account: {_account(view)}."]
        if view["organization_admin"]:
            lines.append("Organization administrator: manages access in every agent.")
        if view["blocked"]:
            lines.append("They can use none of this while "
                         + ("blocked." if view["suspended"] else "their chat account is unavailable.")
                         + " Their grants are kept:")
        for entry in held:
            known = names(entry["hub"])
            lines.append(f"In {entry['hub']}:" + ("" if entry["authoritative"] else
                         " (access still managed in the chat app, so this may not match what "
                         "they can use)"))
            for cap in entry["capabilities"]:
                lines.append(f"- {labelled(entry['hub'], [cap['permission']], known)}: "
                             + ", ".join(_SOURCE_WORDS[s] for s in cap["sources"]))
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
            for t in (my_management_scope, who_has_access, explain_access,
                      propose_access_change, propose_new_account)]
