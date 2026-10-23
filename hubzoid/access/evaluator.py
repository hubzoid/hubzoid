# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""One access check for every way in: ``authorize`` and ``check_run``.

Chat, Settings, the API (session or personal API key), the CLI, MCP,
workflows, schedules, channels and every tool call ask the same question here,
so no surface has its own rule. There is no administrator bypass: an
administrator is allowed because they hold the grant (organization
``manage_access`` or the hub's grants), and a store error refuses (fail closed).

The decision, in a fixed order:

  1. the account: it exists, is active (not pending or deleted), and its
     credential counter (``auth_epoch``) is the one this run or key started
     under, so "revoke all credentials", a password reset, a block or a
     deletion ends work in flight at its next step;
  2. the person is not blocked (access store suspension);
  3. a personal API key limited to some agents reaches only those;
  4. the grant: the hub permission (``use_hub``, a tool's permission), or a
     platform key from ``hubzoid.control.authz`` (organization or hub
     administration).

Speed. A decision is cached per process, keyed by the deployment, the person,
the permission, the hub, the access store's revision and the account's
counter. Each check reads two indexed values (the revision and the counter);
anything that changes access moves one of them, so a revocation applies on the
next check in every process. Checks run at requests, before every tool call and
when a waiting run resumes, never while text streams.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from .identity import normalize

log = logging.getLogger("hubzoid.access")

_CACHE_MAX = 4096
_cache: "OrderedDict[tuple, Decision]" = OrderedDict()
_lock = threading.Lock()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Decision(True, "allowed")


def _remember(key: tuple, decision: Decision) -> Decision:
    with _lock:
        _cache[key] = decision
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return decision


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _account_state(hub_dir: Path, account_id: str | None, email: str) -> tuple[str, int | None]:
    """(status, credential counter) of the account, by id when known."""
    from ..auth import users

    if not account_id and "@" not in email:
        return "missing", None
    st = users.store(Path(hub_dir))
    user = st.get(account_id) if account_id else st.find_by_email(email)
    if user is None:
        return "missing", None
    return user["status"], int(user.get("auth_epoch") or 0)


def _grant_allowed(gs, email: str, hub: str | None, permission: str) -> bool:
    from ..control.authz import CAPABILITIES

    if permission in CAPABILITIES:
        scope = CAPABILITIES[permission]
        if gs.can(email, "*", "manage_access"):
            return True
        if scope == "deployment":
            return False
        if scope == "any_hub":
            return bool(gs.hubs_for(email)) and any(
                gs.can(email, h, "manage_access") for h in gs.hubs_for(email))
        return bool(hub) and gs.can(email, normalize(hub), "manage_access")
    if not hub:
        return False
    return gs.can(email, normalize(hub), permission)


def app_access(hub_dir: Path, app, permission: str | None = None, *, hub: str | None = None,
               tool: str | None = None, server: str | None = None, connector: str | None = None) -> Decision:
    """The decision for a person an embedding app vouched for
    (``identity.AppScope``): the app is still registered and on, the agent is
    within the token's access and the app's current maximum, and so is the
    tool or permission asked about. Grants are never read: such a caller has
    no account and no grants. Platform capabilities are never theirs."""
    from ..control.authz import CAPABILITIES
    from ..embed import config as apps_config
    from ..embed.caller import effective, tool_allowed
    from .store import USE_HUB

    try:
        reg = apps_config.service_for(Path(hub_dir)).get(app.app_id)
    except Exception:  # noqa: BLE001 - can't decide: refuse, never guess
        log.exception("access: apps settings unavailable; denying")
        return Decision(False, "store_error")
    if reg is None or not reg.enabled:
        return Decision(False, "credential_ended")
    access = effective(reg, apps_config.Access(agents=frozenset(app.agents), tools=frozenset(app.tools)))
    agent = normalize(hub or Path(hub_dir).name)
    if agent not in access.agents:
        return Decision(False, "missing_grant")
    if tool is not None or server is not None or connector is not None:
        allowed = tool_allowed(access, tool or "", server=server, connector=connector) or (
            bool(permission) and permission != USE_HUB and permission in access.tools)
        return ALLOW if allowed else Decision(False, "app_tool")
    if not permission or permission == USE_HUB:
        return ALLOW
    if permission in CAPABILITIES:
        return Decision(False, "missing_grant")
    if permission.startswith("connector_"):
        return ALLOW if tool_allowed(access, "", connector=permission[len("connector_"):]) else \
            Decision(False, "missing_grant")
    return ALLOW if access.all_tools() or permission in access.tools else Decision(False, "missing_grant")


def authorize(hub_dir: Path, who, permission: str, *, hub: str | None = None,
              account_id: str | None = None, epoch: int | None = None,
              key_hubs: "tuple[str, ...] | None" = None) -> Decision:
    """May ``who`` (an email, an ``AuthUser`` or an ``Identity``) do
    ``permission`` in ``hub``? ``epoch``: the credential counter the run or
    key started under (checked when given)."""
    app = getattr(who, "app", None)
    if app is not None:
        return app_access(hub_dir, app, permission, hub=hub)
    email = normalize(getattr(who, "email", None) or getattr(who, "user", None) or
                      (who if isinstance(who, str) else ""))
    account_id = account_id or getattr(who, "account_id", None) or (
        getattr(who, "id", None) if hasattr(who, "email") else None)
    if epoch is None:
        epoch = getattr(who, "auth_epoch", None)
    if key_hubs is None:
        key_hubs = getattr(who, "hubs", None)
    if not email:
        return Decision(False, "anonymous")
    if key_hubs is not None and hub is not None and normalize(hub) not in key_hubs:
        return Decision(False, "key_scope")
    from . import store_for
    from ..control.deploy import deployment_id

    try:
        gs = store_for(Path(hub_dir))
        status, current_epoch = _account_state(hub_dir, account_id, email)
        revision = gs.revision()
        key = (deployment_id(Path(hub_dir)), email, account_id, permission,
               normalize(hub) if hub else None, revision, current_epoch, epoch)
        hit = _cache.get(key)
        if hit is not None:
            return hit
        # A subject with no Hubzoid account (a channel sender known only to
        # the access store, an Open WebUI account, a legacy service subject)
        # is judged on its grants alone, as before.
        if status not in ("active", "missing"):
            return _remember(key, Decision(False, "account_inactive"))
        if epoch is not None and current_epoch is not None and int(epoch) != int(current_epoch):
            return _remember(key, Decision(False, "credentials_revoked"))
        if gs.is_suspended(email):
            return _remember(key, Decision(False, "blocked"))
        allowed = _grant_allowed(gs, email, hub, permission)
        return _remember(key, ALLOW if allowed else Decision(False, "missing_grant"))
    except Exception:  # noqa: BLE001 - can't decide: refuse, never guess
        log.exception("access: authorize(%s) unavailable; denying", permission)
        return Decision(False, "store_error")


def credential_live(hub_dir: Path, kind: str, credential_id: str, *,
                    expired_ok: bool = False) -> bool:
    """Whether the browser session or API key that started a run still works:
    not signed out, revoked or expired (``expired_ok``: only not signed out or
    revoked). One indexed read, never cached, so a revoke stops the next tool
    call."""
    import time

    import sqlalchemy as sa

    from ..auth.schema import api_keys, engine_for, sessions

    now = 0 if expired_ok else time.time()
    if kind == "session":
        q = sa.select(sessions.c.token_hash).where(
            sessions.c.token_hash.like(credential_id + "%"), sessions.c.revoked_at.is_(None),
            sessions.c.expires_at > now)
    elif kind == "api_key":
        q = sa.select(api_keys.c.id).where(
            api_keys.c.id == credential_id, api_keys.c.revoked_at.is_(None),
            sa.or_(api_keys.c.expires_at.is_(None), api_keys.c.expires_at > now))
    else:
        return True
    with engine_for(Path(hub_dir)).connect() as conn:
        return conn.execute(q.limit(1)).first() is not None


def still_valid(hub_dir: Path, account_id: str, credential: dict | None, *,
                expired_ok: bool = False) -> bool:
    """For work started later on someone's behalf (a queued message, a reply
    resumed after an approval): the account's credential counter is the one
    recorded then, and the session or key it came with still works
    (``expired_ok``: wasn't signed out or revoked; for a reply resumed after
    an approval that may come days later)."""
    if not credential:
        return True
    from ..auth import users

    epoch = credential.get("epoch")
    if epoch is not None and users.epoch(Path(hub_dir), account_id) != int(epoch):
        return False
    kind, cid = credential.get("kind"), credential.get("id")
    return not (kind and cid) or credential_live(hub_dir, kind, cid, expired_ok=expired_ok)


def check_run(hub_dir: Path, ident, *, tool: str | None = None, server: str | None = None,
              connector: str | None = None) -> Decision:
    """Before a tool call: may the run's account still use this hub, and does
    the session or key that started it still work? An anonymous caller (an
    unverified channel, a test) is left to the tool's own permission, as
    before. Tools never ask a person: allowed means it runs.

    For a person an embedding app vouched for, the tool itself must be within
    their access too (``tool``, or the MCP ``server`` or ``connector`` it
    comes from)."""
    if ident is None or getattr(ident, "user", None) is None:
        return ALLOW
    app = getattr(ident, "app", None)
    if app is not None:
        return app_access(hub_dir, app, tool=tool, server=server, connector=connector)
    from .store import USE_HUB

    decision = authorize(hub_dir, ident, USE_HUB, hub=Path(hub_dir).name)
    credential = getattr(ident, "credential", None)
    if decision.allowed and credential:
        try:
            if not credential_live(hub_dir, *credential):
                return Decision(False, "credential_ended")
        except Exception:  # noqa: BLE001 - can't decide: refuse, never guess
            log.exception("access: credential check unavailable; denying")
            return Decision(False, "store_error")
    return decision
