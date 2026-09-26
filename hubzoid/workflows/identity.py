# Hubzoid workflows. Apache-2.0 licensed like the rest of the repository.
"""Who a scheduled run acts as: an ordinary Hubzoid account, never a service type.

Precedence, first match wins:

  1. `run_as` on the declaration (`@workflow(run_as=...)`, or `run_as:` in a
     `schedule/*.md` frontmatter);
  2. HUBZOID_WORKFLOW_USER from the hub (`<hub>/.env`, or a hub secret);
  3. HUBZOID_WORKFLOW_USER from the deployment (gateway environment or secret);
  4. the setup default: the configured initial owner, recorded once when
     Hubzoid provisions that owner (`GrantStore.provision_owner`), on
     Console-managed hubs only. Local quickstart (authentication off, no
     deployment) is `admin@localhost`.

The email must resolve to a usable account: a bound identity row (an Open WebUI
account id), not pending, not blocked or replaced, and on a Console-managed hub
holding `use_hub`. Anything else raises `IdentityError` with the fix. There is
no fallback to another person, ever.

`run_as` selects an identity; it grants nothing. It is read only from files and
operator configuration, never from an API, a tool or a model argument.

Legacy hubs (access still managed in the chat app) switch only on explicit
configuration (`run_as` or HUBZOID_WORKFLOW_USER), never on the setup default.
With neither, the run keeps its old service identity (`workflow:<name>`,
`workflow:md:<task>`), which holds no restricted access there, and features
that need a person (publishing, email, personal connections) refuse.

A run resolves its identity once, as a checkpointed step, and re-checks the
account before every protected operation (`recheck`), so a configuration change
never switches the person mid-run and a blocked account stops at the next call.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("hubzoid.workflows")

WORKFLOW_USER_ENV = "HUBZOID_WORKFLOW_USER"
LEGACY_SOURCE = "legacy-service"
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+$")


class IdentityError(RuntimeError):
    """The run has no usable account. The message says what to change."""


@dataclass(frozen=True)
class RunIdentity:
    """The account a run acts as, captured when the run starts.

    `subject` is the normalized email (or, for a legacy hub with nothing
    configured, the old service subject). `account_id` is the Open WebUI
    account id at resolution time, so a later account replacement is caught.
    `source` says which rule chose it."""

    subject: str
    account_id: str | None
    source: str
    email: str | None = None
    legacy_permissions: tuple[str, ...] = ()

    @property
    def is_person(self) -> bool:
        return self.source != LEGACY_SOURCE

    def to_dict(self) -> dict:
        d = asdict(self)
        d["legacy_permissions"] = list(self.legacy_permissions)
        return d

    @staticmethod
    def from_dict(data: dict) -> "RunIdentity":
        return RunIdentity(
            subject=data["subject"], account_id=data.get("account_id"),
            source=data.get("source", "run_as"), email=data.get("email"),
            legacy_permissions=tuple(data.get("legacy_permissions") or ()),
        )


def validate_run_as(value) -> str:
    """Syntax check at declaration time (the account is checked per run)."""
    if not isinstance(value, str) or not _EMAIL.match(value.strip()):
        raise ValueError(f"run_as must be an account email, got {value!r}")
    return value.strip().lower()


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def local_quickstart(hub_dir: Path) -> bool:
    """Authentication off and no deployment: the single-account local mode."""
    from .. import deployment

    try:
        registered = bool(deployment.read(Path(hub_dir)))
    except Exception:  # noqa: BLE001 — an unreadable manifest is not local mode
        registered = True
    return not _truthy(os.environ.get("WEBUI_AUTH")) and not registered


def configured(hub_dir: Path, *, hub: str | None = None,
               run_as: str | None = None) -> tuple[str | None, str]:
    """(email, source) chosen by configuration, before any account check."""
    from ..access import normalize, store_for
    from ..access.session import LOCAL_OWNER

    if run_as:
        return normalize(run_as), "run_as"
    hub_dir = Path(hub_dir)
    try:
        from dotenv import dotenv_values

        hub_value = (dotenv_values(hub_dir / ".env").get(WORKFLOW_USER_ENV) or "").strip()
    except Exception:  # noqa: BLE001 — an unreadable .env is reported by settings
        hub_value = ""
    # The hub layer wins. A bridge has already loaded <hub>/.env (and any hub
    # secret) over the deployment environment; a CLI command may not have, so
    # the file is consulted directly as well.
    if hub_value:
        return normalize(hub_value), "hub"
    env_value = (os.environ.get(WORKFLOW_USER_ENV) or "").strip()
    if env_value:
        return normalize(env_value), "deployment"
    # The setup default applies only where Hubzoid manages access. A legacy hub
    # (access still in the chat app) switches only on explicit configuration,
    # so recording an owner never silently changes how its tasks run.
    gs = store_for(hub_dir)
    hub_key = hub or hub_dir.name
    default = gs.workflow_default(hub_key)
    if default and gs.is_authoritative(hub_key):
        return normalize(default), "setup"
    if local_quickstart(hub_dir):
        return LOCAL_OWNER, "local"
    return None, "none"


def _missing_default_message(hub_dir: Path, hub: str, what: str) -> str:
    from ..access.session import configured_owner

    owner = configured_owner(hub_dir)
    setup = (f"sign in once as the configured owner ({owner}) so setup records it"
             if owner else "set HUBZOID_GATEWAY_ADMIN_EMAIL to the owner account and sign in once")
    return (f"{what} in hub {hub!r} has no account to run as. Add run_as to its "
            f"declaration, set {WORKFLOW_USER_ENV}=<account email> in the hub or "
            f"deployment configuration, or {setup}.")


def check(hub_dir: Path, hub: str, email: str, source: str, *,
          account_id: str | None = None, what: str = "This workflow") -> RunIdentity:
    """The usable account for `email`, or IdentityError naming the fix."""
    from ..access import normalize, store_for
    from ..access.session import LOCAL_OWNER
    from ..access.store import USE_HUB

    email = normalize(email)
    if not _EMAIL.match(email):
        raise IdentityError(f"{what} is configured to run as {email!r}, which is not an account email.")
    gs = store_for(hub_dir)
    where = {"run_as": "its run_as", "hub": f"the hub's {WORKFLOW_USER_ENV}",
             "deployment": f"the deployment's {WORKFLOW_USER_ENV}",
             "setup": "the setup default (the initial owner)",
             "local": "the local quickstart account"}.get(source, source)
    ident = gs.identity(email) or {}
    with gs._engine.connect() as conn:  # noqa: SLF001 — read-only marker lookup
        suspended = gs._meta_get(conn, "suspended:" + email) == "1"
        unavailable = gs._meta_get(conn, "account_unavailable:" + email) == "1"
    if suspended:
        raise IdentityError(
            f"{what} runs as {email} ({where}), and that account is blocked or was "
            "replaced. Reactivate it in the Console, or change the configured account. "
            "Hubzoid never falls back to another person.")
    local = email == LOCAL_OWNER and local_quickstart(hub_dir)
    if not ident.get("owui_id"):
        if local:
            return RunIdentity(email, None, source, email)
        raise IdentityError(
            f"{what} runs as {email} ({where}), but no signed-in account exists for "
            "that email in this deployment. Create the account (or have that person "
            "sign in once), or change the configured account.")
    if ident.get("pending") or unavailable:
        raise IdentityError(
            f"{what} runs as {email} ({where}), but that account is awaiting approval "
            "or was removed from the chat app. Approve or restore it, then refresh "
            "accounts in the Console.")
    if account_id and ident["owui_id"] != account_id:
        raise IdentityError(
            f"The account behind {email} changed while this run was in progress. The "
            "run stops rather than continue as a different account.")
    try:
        managed = gs.is_authoritative(hub)
        allowed = (not managed) or gs.can(email, hub, USE_HUB)
    except Exception as exc:  # noqa: BLE001 — fail closed
        raise IdentityError(f"Access data is unavailable, so {what.lower()} did not run.") from exc
    if not allowed:
        raise IdentityError(
            f"{what} runs as {email} ({where}), but that account has no access to "
            f"hub {hub!r}. Grant 'Use this agent' in the Console, or change the "
            "configured account.")
    return RunIdentity(email, ident["owui_id"], source, ident.get("email") or email)


def resolve(hub_dir: Path, *, hub: str | None = None, run_as: str | None = None,
            legacy_subject: str | None = None, what: str = "This workflow") -> RunIdentity:
    """Resolve the account a new run acts as (see module docstring)."""
    from ..access import store_for

    hub_dir = Path(hub_dir)
    hub = (hub or hub_dir.name).lower()
    email, source = configured(hub_dir, hub=hub, run_as=run_as)
    if not email:
        try:
            managed = store_for(hub_dir).is_authoritative(hub)
        except Exception as exc:  # noqa: BLE001
            raise IdentityError(f"Access data is unavailable, so {what.lower()} did not run.") from exc
        if not managed and legacy_subject:
            log.warning("%s: %s", legacy_subject,
                        _missing_default_message(hub_dir, hub, what)
                        + " Until then it keeps its legacy service identity, which cannot "
                          "publish, email or use personal connections.")
            return RunIdentity(legacy_subject, None, LEGACY_SOURCE)
        raise IdentityError(_missing_default_message(hub_dir, hub, what))
    ident = check(hub_dir, hub, email, source, what=what)
    if legacy_subject:
        missing = legacy_permissions_not_held(hub_dir, hub, legacy_subject, ident.subject)
        if missing:
            log.warning(
                "%s held %s before workflows ran as people; it now runs as %s, who does "
                "not. Grant those to %s in the Console (or set run_as to an account that "
                "holds them). Legacy grants are not carried over.",
                legacy_subject, ", ".join(missing), ident.subject, ident.subject)
            ident = RunIdentity(ident.subject, ident.account_id, ident.source, ident.email,
                                tuple(missing))
    return ident


def recheck(hub_dir: Path, hub: str, ident: RunIdentity, *, what: str = "This workflow") -> None:
    """Before a protected operation: is the run's account still usable?"""
    if not ident.is_person:
        return
    check(hub_dir, hub, ident.subject, ident.source, account_id=ident.account_id, what=what)


def require_person(ident: RunIdentity, feature: str) -> None:
    if not ident.is_person:
        raise IdentityError(
            f"{feature} needs the run to act as a person. Set run_as on the declaration "
            f"or {WORKFLOW_USER_ENV} in the hub or deployment configuration.")


def legacy_permissions_not_held(hub_dir: Path, hub: str, legacy_subject: str,
                                subject: str) -> list[str]:
    """Permissions the old `workflow:*` subject holds in `hub` that `subject`
    does not. Reported, never granted."""
    from ..access import store_for
    from ..access.store import USE_HUB

    try:
        gs = store_for(hub_dir)
        before = gs.permissions_for(legacy_subject, hub) - {USE_HUB, "*"}
        return sorted(before - gs.permissions_for(subject, hub))
    except Exception:  # noqa: BLE001 — a report, never a gate
        log.exception("workflows: could not compare legacy grants for %s", legacy_subject)
        return []


def describe(hub_dir: Path, *, hub: str | None = None, run_as: str | None = None,
             legacy_subject: str | None = None) -> str:
    """One line for `hubzoid schedule list`: who a workflow runs as, or why not."""
    try:
        ident = resolve(hub_dir, hub=hub, run_as=run_as, legacy_subject=legacy_subject)
    except IdentityError as exc:
        return f"cannot run: {exc}"
    if not ident.is_person:
        return f"legacy service identity {ident.subject} (set run_as or {WORKFLOW_USER_ENV})"
    return f"{ident.subject} ({ident.source})"


def person_slug(subject: str) -> str:
    """A filesystem-safe, collision-resistant name for one person's folders."""
    base = re.sub(r"[^a-z0-9._-]+", "-", subject.lower()).strip("-.")[:40] or "person"
    return f"{base}-{hashlib.sha256(subject.encode()).hexdigest()[:8]}"


def markdown_scratch(hub_dir: Path, task_name: str, ident: RunIdentity) -> str:
    """The hub-relative scratch folder a markdown task run may use.

    Each person gets their own folder, `.hubzoid/schedule/<task>@<person>`, a
    sibling of the task's historical folder, so no run can read or write
    another person's state. The historical folder (`.hubzoid/schedule/<task>`)
    is left as it was: it belongs to no person, and only a legacy service run
    (a legacy hub with no account configured) keeps using it."""
    base = f".hubzoid/schedule/{task_name}"
    if not ident.is_person:
        return base
    return f"{base}@{person_slug(ident.subject)}"
