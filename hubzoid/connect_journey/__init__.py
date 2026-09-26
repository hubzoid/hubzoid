"""Connection journeys: a personal link that connects the caller's account for
an app (for example Gmail), confirms the verified result on a browser page and
back in the chat that asked. Pages live under `/portal/connect/`.

An app is connected through the OAuth 2.1 MCP server registered for it in Open
WebUI (``OWUI_NATIVE_MCP``). An app with no such server is not available to
connect.

The journey, end to end:

  1. The agent calls ``connect_account(app)`` (`tools/connect_tools.py`). The
     tool resolves the one Open WebUI server for the app, checks the
     ``connector_<app>`` capability through ``guard.decide`` (surface gate
     included) and asks Open WebUI whether the caller is connected already.
     If not, :func:`start` records a short-lived journey bound to the trusted
     caller and returns ``<public>/portal/connect/<id>``, never a provider URL.
  2. The link page requires a signed-in Open WebUI session whose email is the
     journey's subject (`web.py`). ``Start`` sends the browser to Open WebUI's
     authorize route.
  3. After consent the browser returns to ``/portal/connect/<id>/done``, which
     asks Open WebUI whether *this* journey connected. Callback parameters
     are never read.
  4. The originating hub's inbound process confirms the result in WhatsApp,
     once (`notify.py`), and may offer a one-use "Reply YES" continuation.

Off unless ``HUBZOID_CONNECT_JOURNEY`` is on for the hub (the tool is not
registered and no journey can be created). The pages are always mounted but
only ever serve journeys a hub with the switch on created.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from fastapi import APIRouter

from ..access.identity import normalize
from . import store
from .providers import JourneyError, label

log = logging.getLogger("hubzoid.connect")

_TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_TTL = 600
_SURFACE_REASONS = {
    "anonymous": "Connecting an account needs a signed-in person.",
    "blocked": "Your access to this agent is blocked. Contact your administrator.",
    "store-error": "Access could not be checked right now. Try again shortly.",
}

__all__ = [
    "JourneyError", "attach_continuation", "build_router", "capability", "enabled",
    "finalize", "label", "link_url", "permissions", "start", "take_continuation",
]


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get("HUBZOID_CONNECT_JOURNEY") or "").strip().lower() in _TRUTHY


def ttl(env=None) -> int:
    """``HUBZOID_CONNECT_TTL`` seconds (default 600, kept within 60..3600)."""
    env = os.environ if env is None else env
    try:
        value = int((env.get("HUBZOID_CONNECT_TTL") or "").strip() or DEFAULT_TTL)
    except ValueError:
        value = DEFAULT_TTL
    return max(60, min(3600, value))


def app_key(name: str) -> str:
    from ..owui_mcp import app_key as _key

    return _key(name)


def capability(app: str) -> str:
    from ..owui_mcp import capability as _cap

    return _cap(app)


def public_base(env=None) -> str:
    """The deployment's public root (where `/portal` and Open WebUI are served).

    ``WEBUI_URL`` first (the chat app's public URL, which OAuth needs anyway),
    then ``HUBZOID_PUBLIC_URL`` without a gateway's per-bridge ``/b/<slug>``.
    """
    env = os.environ if env is None else env
    base = (env.get("WEBUI_URL") or "").strip().rstrip("/")
    if not base:
        base = (env.get("HUBZOID_PUBLIC_URL") or "").strip().rstrip("/")
        head, sep, tail = base.rpartition("/b/")
        if sep and tail and "/" not in tail:
            base = head
    if not base:
        base = f"http://localhost:{(env.get('PORT') or '3080').strip()}"
    return base


def link_path(jid: str) -> str:
    return f"/portal/connect/{jid}"


def link_url(jid: str) -> str:
    return public_base() + link_path(jid)


# ---------------------------------------------------------------------------
# Router and capability catalog (contracts used by the portal)
# ---------------------------------------------------------------------------
def build_router(hub_dir: Path, *, session_email=None) -> APIRouter:
    """``/portal/connect/{id}``, ``/{id}/start`` (POST), ``/{id}/cancel`` (POST),
    ``/{id}/done`` and ``/{id}/status``. ``session_email`` replaces the Open
    WebUI session check in tests."""
    from .web import build_router as _build

    return _build(Path(hub_dir), session_email=session_email)


def permissions(hub_dir: Path) -> list[dict]:
    """Connector capabilities (`connector_<app>`) this hub offers: one per Open
    WebUI OAuth MCP server when ``OWUI_NATIVE_MCP`` is on (a managed hub needs
    these grants for per-turn injection too)."""
    from .. import owui_mcp
    from ..access import owui_tool_servers as servers

    hub_dir = Path(hub_dir)
    out: dict[str, dict] = {}
    if owui_mcp.enabled():
        for c in servers.list_mcp_connections(hub_dir):
            if c.get("auth_type") not in servers.OAUTH_AUTH_TYPES:
                continue
            app = owui_mcp.app_key(c["id"])
            if app:
                out.setdefault(app, _perm(app, c.get("name") or label(app)))
    return [out[k] for k in sorted(out)]


def _perm(app: str, name: str) -> dict:
    return {"permission": capability(app), "label": f"Connect {name}",
            "description": f"Connect and use their own {name} account through this agent.",
            "sensitive": True}


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------
def may_start(hub_dir, ident, app: str) -> tuple[bool, str]:
    """May this caller start a journey for ``app`` here? ``guard.decide`` on
    ``connector_<app>``: the surface gate, then the Console grant (managed hub)
    or the legacy group of the same name."""
    from ..access.guard import decide

    return decide(Path(hub_dir), ident, capability(app))


def _capability_label(hub_dir, app: str) -> str:
    """The name the Console shows for `connector_<app>`, so a person can ask
    for exactly what their administrator sees."""
    try:
        for row in permissions(Path(hub_dir)):
            if row.get("permission") == capability(app) and row.get("label"):
                return row["label"]
    except Exception:  # noqa: BLE001 — wording only
        log.debug("connect: no label for %s", app, exc_info=True)
    return f"Connect {label(app)}"


def _denied_message(hub_dir, app: str, reason: str) -> str:
    if reason in _SURFACE_REASONS:
        return _SURFACE_REASONS[reason]
    if reason.startswith("surface:"):
        return f"Connecting {label(app)} is not available on this channel."
    try:
        from ..access import store_for

        managed = store_for(Path(hub_dir)).is_authoritative(Path(hub_dir).name.lower())
    except Exception:  # noqa: BLE001 — wording only
        managed = True
    if not managed:
        # Legacy hubs still grant through a chat-app group of the capability's name.
        return (f"You do not have permission to connect {label(app)} here. Ask your administrator "
                f"to add you to the chat-app group {capability(app)}.")
    return (f"You do not have permission to connect {label(app)} here. Ask an administrator "
            f"of this agent to grant you \"{_capability_label(hub_dir, app)}\" in the Admin Console.")


# ---------------------------------------------------------------------------
# Start (the connect_account tool) and finalize (pages, poller)
# ---------------------------------------------------------------------------
def start(hub_dir: Path, *, app: str, reconnect: bool = False,
          now: float | None = None) -> dict:
    """Begin a journey for the current caller (``current_identity()``).

    Returns ``{"state": "connected", "app", "label"}`` when the caller is
    connected already (and ``reconnect`` is false), else ``{"state": "link",
    "url", "id", "app", "label", "expires_in", "subject"}``. Raises JourneyError
    (``anonymous``, ``unknown-app``, ``denied``, ``conflict``, ``unavailable``)
    with a message safe to show the person.
    """
    from .. import _request_ctx
    from ..access.identity import current_identity
    from . import providers

    hub_dir = Path(hub_dir)
    hub = normalize(hub_dir.name)
    now = time.time() if now is None else now
    ident = current_identity()
    key = app_key(app)
    if ident.is_anonymous:
        raise JourneyError("anonymous", _SURFACE_REASONS["anonymous"])
    if not key:
        raise JourneyError("unknown-app", "Name the app to connect, for example Gmail.")
    subject = normalize(ident.user)
    # An app with no Open WebUI server is simply not available here: say so
    # before asking for a capability no one could grant. Everything else about
    # the setup (such as a duplicate server) is told only to permitted callers.
    providers.require_available(hub_dir, key)
    allowed, reason = may_start(hub_dir, ident, key)
    if not allowed:
        store.audit(hub_dir, hub=hub, subject=subject, surface=ident.surface, app=key,
                    decision="deny", reason=reason)
        raise JourneyError("denied", _denied_message(hub_dir, key, reason))
    provider = providers.for_app(hub_dir, key)

    try:
        status = provider.status(subject)
    except JourneyError:
        raise
    except Exception:  # noqa: BLE001 — a provider outage must not look connected
        log.warning("connect: %s status check failed for %s", key, subject, exc_info=True)
        raise JourneyError("unavailable", f"{label(key)} could not be checked right now. "
                                          "Try again shortly.")
    chat_id = _request_ctx.get_chat_id()
    surface = ident.surface
    if status == "connected" and not reconnect:
        # Finish any journey that led here, through the same provider check.
        # This reply already tells this chat, so its confirmation is not sent
        # again. Other chats still get theirs from the outbox.
        for j in store.open_for(hub_dir, subject=subject, app=key):
            j = finalize(hub_dir, j, provider=provider, now=now)
            if j["status"] == "connected" and (j["surface"], j["chat_id"]) == (surface, chat_id):
                store.claim_notify(hub_dir, j["id"], now=now)
        return {"state": "connected", "app": key, "label": label(key)}

    handle = None
    if chat_id and surface in ("whatsapp", "telegram") and chat_id.startswith(f"{surface}-"):
        handle = chat_id[len(surface) + 1:]
    jid = store.new_id()
    life = ttl()
    _row, superseded = store.create(
        hub_dir, jid=jid, hub=hub, subject=subject, surface=surface, chat_id=chat_id,
        handle=handle, app=key, provider=provider.name, provider_ref=provider.server_id,
        ttl=life, now=now)
    for old in superseded:
        store.audit(hub_dir, hub=old["hub"], subject=subject, surface=old["surface"],
                    app=key, decision="superseded", reason="newer link")
    store.audit(hub_dir, hub=hub, subject=subject, surface=surface, app=key,
                decision="allow", reason="reconnect link" if reconnect else "link")
    return {"state": "link", "url": link_url(jid), "id": jid, "app": key,
            "label": label(key), "expires_in": life, "subject": subject}


def finalize(hub_dir, journey: dict, *, provider=None,
             now: float | None = None) -> dict:
    """Bring ``journey`` up to date from its provider and return the fresh row.

    ``started`` becomes ``connected`` or ``failed`` only on the provider's own
    answer, and ``expired`` once its window passes without one. ``pending``
    expires. A started journey with an unknown provider is left as it is.
    """
    from . import providers

    now = time.time() if now is None else now
    j = journey
    if j["status"] == "pending" and now >= j["expires"]:
        if store.transition(hub_dir, j["id"], frm=("pending",), to="expired", now=now):
            store.audit(hub_dir, hub=j["hub"], subject=j["subject"], surface=j["surface"],
                        app=j["app"], decision="expired", reason="link not opened")
    elif j["status"] == "started":
        prov = provider if provider is not None else providers.for_journey(hub_dir, j)
        result = "pending"
        if prov is not None:
            try:
                result = prov.verify(j)
            except Exception:  # noqa: BLE001 — unknown is not success
                log.warning("connect: could not verify %s for %s", j["app"], j["subject"],
                            exc_info=True)
        if result == "connected":
            if store.transition(hub_dir, j["id"], frm=("started",), to="connected", now=now):
                store.audit(hub_dir, hub=j["hub"], subject=j["subject"], surface=j["surface"],
                            app=j["app"], decision="connected", reason="verified with provider")
        elif result == "failed":
            if store.transition(hub_dir, j["id"], frm=("started",), to="failed", now=now):
                store.audit(hub_dir, hub=j["hub"], subject=j["subject"], surface=j["surface"],
                            app=j["app"], decision="failed", reason="provider reported failure")
        elif now >= j["expires"] and prov is not None:
            if store.transition(hub_dir, j["id"], frm=("started",), to="expired", now=now):
                store.audit(hub_dir, hub=j["hub"], subject=j["subject"], surface=j["surface"],
                            app=j["app"], decision="expired", reason="not completed")
    return store.get(hub_dir, j["id"]) or j


# ---------------------------------------------------------------------------
# Continuation (contracts used by the inbound harness)
# ---------------------------------------------------------------------------
def attach_continuation(hub_dir, *, subject, surface, chat_id, since: float, text: str) -> bool:
    """Attach a turn's verbatim user text to the journey that turn created."""
    return store.attach_continuation(hub_dir, subject=subject, surface=surface,
                                     chat_id=chat_id, since=since, text_=text)


def take_continuation(hub_dir, *, subject, surface, chat_id) -> str | None:
    """Claim the offered continuation for this sender and chat. Atomic, single
    use. The caller dispatches it as a fresh turn, so identity, entry and
    capability checks all run again."""
    return store.take_continuation(hub_dir, subject=subject, surface=surface, chat_id=chat_id)


def decline_continuation(hub_dir, *, subject, surface, chat_id) -> int:
    return store.decline_continuation(hub_dir, subject=subject, surface=surface, chat_id=chat_id)
