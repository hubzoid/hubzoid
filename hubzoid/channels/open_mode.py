"""Open mode: "Anyone in this Slack workspace" for one channel connection.

Off by default. An organization administrator turns it on for one agent
connection (Console, the channel's Access card, or ``hubzoid channels open``).
While it is on, a person who writes to the bot and has no account link is
resolved like this:

1. They must be a member of the connection's own Slack workspace: Slack's
   ``users.info`` says the same team, and they are not a bot, an app user, a
   deleted user, a Slack Connect stranger, or one of Slack's own restricted or
   single-channel guests. Anyone else is refused as unlinked.
2. Their Slack profile email, when Slack has confirmed it and the app may read
   it (``users:read.email``), is matched to a Hubzoid account. An active,
   unblocked account runs as itself with its normal access: open mode stands
   in for the channel's "Start work from" grant and nothing else. An account
   that is blocked, pending, or lacks this agent is refused, never downgraded.
   Such a person was never vouched for by an administrator's link, so their
   runs use the ``slack-channel`` or ``slack-dm`` surface: restricted tools
   stay off unless the operator opted ``slack-dm`` in, as in 1.x.
3. Everyone else is a **Slack guest**: no account, no grants. Their
   conversations belong to a private subject (``slackguest_<digest>``, no
   ``@``), and their runs carry an ``AppScope`` (``channel:<id>``) whose access
   comes from :func:`guest_access` alone: unrestricted hub knowledge and a few
   built-in read tools, nothing that changes anything, no approvals, no
   workflows, and a daily cap per person.

The setting lives in the operational database (``hz_channel_open_mode``), never
in a file, and is bound to the Slack workspace and app it was approved for:
when the connection file later names another workspace or app, open mode
counts as off until an organization administrator turns it on again. Each
change is written to Access changes.

It creates no "everyone signed in" (``*``) grant. It is the one approved
exception to "no new access for everyone" (``access.store.NO_NEW_EVERYONE``),
for one connection only.

How others do it: Agno lets unmapped Slack members run with a configured
``default_role`` (``os/interfaces/slack/security.py``); eve makes every Slack
user a principal scoped to the installation team (``channels/slack/auth.ts``).
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from . import store
from .errors import ChannelError

log = logging.getLogger("hubzoid.channels.open_mode")

GUEST_PREFIX = "slackguest_"
GUEST_SURFACE = "slack-guest"
GUEST_CREDENTIAL = "channel_guest"
DEFAULT_CAP, MIN_CAP, MAX_CAP = 20, 1, 500
#: the built-in tools a guest may use: each a ``read`` in ``tool_classes``,
#: none reaching outside the hub's own knowledge and skills
GUEST_TOOLS = frozenset({"current_time", "list_knowledge", "read_knowledge", "list_skills", "load_skill"})
#: how long a Slack member lookup is kept (successful lookups only)
MEMBER_TTL = 600.0
#: after a Slack outage or rate limit, how long a member is not looked up again
WAIT_TTL = 30.0
#: users.info is quick; a slow answer is an outage, not a reason to stall routing
LOOKUP_TIMEOUT = 5
#: Slack errors that mean "try again later", not "no"
_TRANSIENT = {"ratelimited", "internal_error", "fatal_error", "service_unavailable", "request_timeout"}

# -- the setting --------------------------------------------------------------------

def get(hub_dir, hub: str, cid: str) -> dict | None:
    """The stored setting, or None. Not checked against the connection."""
    with store.engine(Path(hub_dir)).connect() as c:
        row = c.execute(text("SELECT * FROM hz_channel_open_mode WHERE hub=:h AND connection_id=:c"),
                        {"h": hub, "c": cid}).mappings().first()
    return dict(row) if row else None


def _bound(conn) -> tuple[str, str]:
    cfg = conn.settings
    return getattr(cfg, "installation_team_id", "") or "", getattr(cfg, "app_id", "") or ""


def view(conn) -> dict:
    """What Console and the CLI show. ``enabled`` is the effective state;
    ``stale`` says it was approved for another workspace or app."""
    row = get(conn.hub_dir, conn.hub, conn.id) or {}
    team, app = _bound(conn)
    stale = bool(row.get("enabled")) and (row.get("team"), row.get("app")) != (team, app)
    return {"supported": bool(conn.adapter.open_mode) and bool(conn.config.agent),
            "enabled": bool(row.get("enabled")) and not stale, "stale": stale,
            "approved_team": row.get("team") or None,
            "daily_cap": int(row.get("daily_cap") or DEFAULT_CAP), "workspace": team,
            "updated_by": row.get("updated_by"), "updated": row.get("updated")}


def active(conn) -> dict | None:
    """The setting when open mode applies to this connection now: the
    provider supports it, the connection answers with the agent and is
    enabled, and the approval is for the workspace and app it serves."""
    if not conn.adapter.open_mode or not conn.config.agent or not conn.enabled:
        return None
    v = view(conn)
    return v if v["enabled"] else None


def set_open(conn, *, enabled: bool, daily_cap: int | None = None, actor: str) -> dict:
    """Turn open mode on or off and set the guests' daily cap. The caller has
    checked the actor is an organization administrator. Written to Access
    changes."""
    if enabled and not (conn.adapter.open_mode and conn.config.agent):
        raise ChannelError("validation_failed", "Open mode is only for Slack connections that answer with an "
                           "agent.", status=422)
    current = get(conn.hub_dir, conn.hub, conn.id) or {}
    effective = view(conn)
    cap = int(daily_cap if daily_cap is not None else (current.get("daily_cap") or DEFAULT_CAP))
    if not MIN_CAP <= cap <= MAX_CAP:
        raise ChannelError("validation_failed", f"The daily cap is {MIN_CAP} to {MAX_CAP} requests a person.",
                           status=422)
    team, app = _bound(conn)
    # Effective state: turning it on again for another workspace is an "on".
    was = bool(effective["enabled"])
    action = ("channel_open_on" if enabled and not was else "channel_open_off" if was and not enabled
              else "channel_open_cap")
    # The access change is recorded first: no audit row, no change.
    _audit(conn, actor, action, f"slack-workspace:{team}:guest-cap:{cap}")
    with store.engine(conn.hub_dir).begin() as c:
        params = {"h": conn.hub, "c": conn.id, "e": 1 if enabled else 0, "d": cap, "t": team, "a": app,
                  "u": actor, "w": time.time()}
        n = c.execute(text("UPDATE hz_channel_open_mode SET enabled=:e, daily_cap=:d, team=:t, app=:a, "
                           "updated_by=:u, updated=:w WHERE hub=:h AND connection_id=:c"), params).rowcount
        if not n:
            c.execute(text("INSERT INTO hz_channel_open_mode (hub, connection_id, enabled, daily_cap, team, app, "
                           "updated_by, updated) VALUES (:h, :c, :e, :d, :t, :a, :u, :w)"), params)
    return view(conn)


def _audit(conn, actor: str, action: str, detail: str) -> None:
    from ..access import store_for

    try:
        # Access changes: who, what, which connection, and the workspace and cap in `permission`.
        store_for(conn.hub_dir).audit_event(actor, action, subject=f"channel:{conn.id}", hub=conn.hub_dir.name,
                                            permission=detail, surface="console")
    except Exception as exc:  # noqa: BLE001 - fail closed: an unrecorded access change is not made
        log.warning("open mode: could not write the access change for %s", conn.id, exc_info=True)
        raise ChannelError("unavailable", "The access change could not be recorded, so nothing changed. "
                           "Try again shortly.", status=503) from exc


# -- guests -----------------------------------------------------------------------------

def guest_subject(cid: str, team: str, user: str) -> str:
    digest = hashlib.sha256(f"{cid}\0{team}\0{user}".encode("utf-8")).hexdigest()[:40]
    return f"{GUEST_PREFIX}{digest}"


def is_guest_subject(value) -> bool:
    return bool(value) and str(value).startswith(GUEST_PREFIX) and "@" not in str(value)


def guest_access(hub_dir, cid: str):
    """The guest scope's limit for connection ``cid``, or None when open mode
    no longer applies (turned off, paused, another workspace, removed)."""
    from . import config as config_lib

    try:
        conn = config_lib.active(Path(hub_dir), cid)
    except Exception:  # noqa: BLE001 - no connection: no access
        return None
    if active(conn) is None:
        return None
    return {"agents": frozenset({Path(hub_dir).name.lower()}), "tools": GUEST_TOOLS}


def guest_identity(hub_dir, cid: str, subject: str, *, record_id: str | None = None):
    """The run identity of a guest reply: their subject, the ``slack-guest``
    surface and the guest scope. Built at every run start from the current
    setting, so turning open mode off stops queued work and a restart keeps
    the rest working."""
    from ..access.identity import AppScope, Identity

    limit = guest_access(hub_dir, cid) if is_guest_subject(subject) else None
    scope = AppScope(app_id=f"channel:{cid}", agents=limit["agents"] if limit else frozenset(),
                     tools=limit["tools"] if limit else frozenset())
    return Identity.make(subject, (), GUEST_SURFACE, run_identity_id=record_id, app=scope)


# -- Slack membership -------------------------------------------------------------------

_members: dict[tuple, tuple[float, dict]] = {}
_members_lock = threading.Lock()


def _lookup(conn, user_id: str) -> tuple[str, dict | None]:
    """("ok", Slack user) | ("wait", None) | ("no", None). Successful
    lookups are kept for ten minutes; refusals and errors never are."""
    key = (conn.hub, conn.id, user_id)
    now = time.monotonic()
    with _members_lock:
        hit = _members.get(key)
        if hit and hit[0] > now:
            return ("wait", None) if hit[1] is None else ("ok", hit[1])
    from slack_sdk.errors import SlackApiError

    from .slack.adapter import client

    def _wait():
        with _members_lock:
            _members[key] = (now + WAIT_TTL, None)   # don't ask Slack again for a while
        return "wait", None

    try:
        web = client(conn)
        if hasattr(web, "timeout"):
            web.timeout = LOOKUP_TIMEOUT
        info = (web.users_info(user=user_id).get("user") or {})
    except SlackApiError as exc:
        error = (exc.response or {}).get("error") if getattr(exc, "response", None) is not None else None
        return _wait() if error in _TRANSIENT else ("no", None)
    except ChannelError:
        return "no", None
    except Exception:  # noqa: BLE001 - network: try again later
        log.warning("open mode: Slack users.info failed for %s", conn.id, exc_info=True)
        return _wait()
    with _members_lock:
        if len(_members) > 5000:
            _members.clear()
        _members[key] = (now + MEMBER_TTL, info)
    return "ok", info


def clear_cache() -> None:
    with _members_lock:
        _members.clear()


def _member(conn, info: dict) -> bool:
    team = conn.settings.installation_team_id
    return (info.get("team_id") == team and not info.get("deleted") and not info.get("is_bot")
            and not info.get("is_app_user") and not info.get("is_stranger")
            and not info.get("is_restricted") and not info.get("is_ultra_restricted"))


def resolve(conn, event, row: dict | None = None):
    """For a sender with no account link: ``(person, None)`` or ``(None,
    reason)``. ``reason`` is a notice reason, or ``"wait"`` to route the
    delivery again later. A person is either an account (``open=True``) or a
    guest (``guest=True``)."""
    from . import router

    setting = active(conn)
    actor = event.actor
    if setting is None or actor is None or not actor.external_id or actor.is_bot:
        return None, "identity_unmapped"
    team = conn.settings.installation_team_id
    teams = (actor.evidence or {}).get("teams") or {}
    if event.origin.workspace_id != team or any(t != team for k, t in teams.items()
                                                if k in ("user_team", "source_team")):
        return None, "identity_unmapped"
    status, info = _lookup(conn, actor.external_id)
    if status == "wait":
        return None, "wait"
    if status != "ok" or not _member(conn, info or {}):
        return None, "identity_unmapped"
    profile = info.get("profile") or {}
    email = (profile.get("email") or "").strip().lower() if info.get("is_email_confirmed") is True else ""
    if email:
        blocked = _suspended(conn.hub_dir, email)
        if blocked is None:
            return None, "wait"              # the access store did not answer: never a guest by default
        if blocked:
            return None, "blocked"
        from ..auth import users

        user = users.find_by_email(conn.hub_dir, email)
        if user is not None:
            person, reason = router._account_person(conn, user, email)
            if person is None:
                return None, reason
            denied = router._hub_access(conn.hub_dir, person.email)
            if denied == "hub_access_denied":
                return None, "hub_access_denied"
            if denied:
                return None, "wait"
            person.open = True
            person.gate = "slack-dm" if event.data.get("channel_type") == "im" else "slack-channel"
            return person, None
    name = profile.get("display_name") or profile.get("real_name") or info.get("name") or ""
    return SimpleNamespace(guest=True, id=guest_subject(conn.id, team, actor.external_id),
                           email=guest_subject(conn.id, team, actor.external_id), name=str(name)[:80],
                           cap=int(setting["daily_cap"])), None


def _suspended(hub_dir, email: str) -> bool | None:
    """Blocked (True), not (False), or could not tell (None)."""
    from ..access import store_for

    try:
        return bool(store_for(hub_dir).is_suspended(email))
    except Exception:  # noqa: BLE001
        log.warning("open mode: the access store could not check a block", exc_info=True)
        return None


# -- the daily cap -------------------------------------------------------------------------

def _day(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t if t is not None else time.time()))


def reserve(conn, subject: str, delivery_id: str, cap: int) -> bool:
    """Take one of today's requests for this guest, for this delivery. Atomic
    across workers (a lock per guest) and idempotent: a delivery that already
    holds a reservation keeps it, so a retry after a crash neither loses nor
    double-counts it. Reservations live on the delivery rows themselves."""
    day = _day()
    with store.locked(conn.hub_dir, f"guest-cap:{conn.hub}:{conn.id}:{subject}") as c:
        row = c.execute(text("SELECT route_json FROM hz_channel_deliveries WHERE id=:i"),
                        {"i": delivery_id}).first()
        import json

        route = json.loads(row[0]) if row and row[0] else {}
        if route.get("guest_day"):
            return True
        used = c.execute(text("SELECT COUNT(*) FROM hz_channel_deliveries WHERE hub=:h AND connection_id=:c "
                              "AND route_json LIKE :s AND route_json LIKE :d"),
                         {"h": conn.hub, "c": conn.id, "s": f'%"guest":"{subject}"%',
                          "d": f'%"guest_day":"{day}"%'}).scalar() or 0
        if used >= cap:
            return False
        c.execute(text("UPDATE hz_channel_deliveries SET route_json=:r, updated=:t WHERE id=:i"),
                  {"r": store.dumps({**route, "guest": subject, "guest_day": day}), "t": store.now(),
                   "i": delivery_id})
    return True
