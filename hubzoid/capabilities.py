"""Capabilities the Admin Console can grant, in one small registry.

A capability is a permission id a person or service can be granted in a hub,
plus the few facts the Console needs to show it: a label, a group, a help text,
whether it is sensitive, and which settings it needs before it can run.

Two questions stay separate:
  * Availability: is the capability implemented, enabled for this hub and are
    its required settings present? `catalog` answers this locally, by setting
    NAME only. "Configured" means present, not verified with the provider.
  * Authorization: may this person use it? Only the grant store answers this,
    through `access.guard.decide`. Adding a setting never grants anyone access,
    and a grant never creates a setting.

Registering a capability (the whole contract):

    from hubzoid.capabilities import Capability, register

    JEV = register(Capability(
        permission="jev", label="Ask Jev for decisions", group="tools",
        description="Use call_jev in chat for typed decisions from Jev.",
        surfaces=("chat",), requires=("JEV_OPENROUTER_API_KEY",),
        missing="Jev key missing",
    ))
    ...
    registry[ft.name] = access.guard_tool(ft, JEV.permission, hub_dir)

The module that ENFORCES the capability registers it at import and uses
`cap.permission` in its existing `guard_tool` call, so the Console row and the
backend check share one id. Add that module to `REGISTRANTS` so every process
that builds the catalogue imports it. No bespoke Console row is needed.

The catalogue also lists the hub's `restricted/*.py` capabilities (by file name,
never imported), connector capabilities from `connect_journey.permissions`, and
granted ids that no longer exist (obsolete: removable, never grantable).
"""
from __future__ import annotations

import importlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

#: Drawer groups, in display order. Obsolete grants get their own group last.
GROUPS = ("hub", "tools", "restricted", "workflows", "admin")
OBSOLETE_GROUP = "obsolete"
SURFACES = frozenset({"chat", "mcp", "workflow"})
DEFAULTS = ("grant", "included")

#: Modules that register capabilities when imported. `catalog` imports them
#: first, so the registry is complete in every process that asks.
REGISTRANTS: tuple[str, ...] = ("hubzoid.tools", "hubzoid.tools.curator")

NOT_CHECKED = "Not checked"
DISABLED = "Disabled for this hub"
NO_LONGER_AVAILABLE = "No longer available"

_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TRUTHY = ("1", "true", "yes", "on")


def _norm(value) -> str:
    # Same rule as access.identity.normalize, without importing the access
    # package here (tool modules import this one at import time).
    return (value or "").strip().lower() if isinstance(value, str) else ""


def _title(permission: str) -> str:
    return permission.replace("_", " ").title()


@dataclass(frozen=True)
class Capability:
    permission: str                  # stable id; grants refer to it; never rename
    label: str
    group: str                       # hub | tools | restricted | workflows | admin
    description: str = ""            # help tooltip text
    surfaces: tuple[str, ...] = ()   # implemented surfaces only: chat, mcp, workflow
    requires: tuple[str, ...] = ()   # setting NAMES that must be present
    missing: str = "Not configured"  # short status when a required setting is absent
    enabled_by: str = ""             # optional hub switch; present and false -> disabled
    default: str = "grant"           # grant: explicit grant | included: comes with use_hub
    sensitive: bool = False
    delegate_grantable: bool = True  # False: only organization administrators grant it

    def __post_init__(self):
        if not _ID.match(self.permission or ""):
            raise ValueError(f"capability id {self.permission!r} must be lowercase letters, digits and _")
        if not (self.label or "").strip():
            raise ValueError(f"capability {self.permission!r} needs a label")
        if self.group not in GROUPS:
            raise ValueError(f"capability {self.permission!r}: group must be one of {', '.join(GROUPS)}")
        if self.default not in DEFAULTS:
            raise ValueError(f"capability {self.permission!r}: default must be grant or included")
        unknown = set(self.surfaces) - SURFACES
        if unknown:
            raise ValueError(f"capability {self.permission!r}: unknown surface {sorted(unknown)}")
        if self.sensitive and self.default == "included":
            raise ValueError(f"capability {self.permission!r} is sensitive, so it needs an explicit grant")


_registry: dict[str, Capability] = {}


def register(cap: Capability) -> Capability:
    """Add `cap` to the registry and return it. Called at import by the module
    that enforces it. Registering the same capability twice is harmless; a
    different capability under a taken id is an error, never a silent swap."""
    existing = _registry.get(cap.permission)
    if existing is not None and existing != cap:
        raise ValueError(f"capability {cap.permission!r} is already registered differently")
    _registry[cap.permission] = cap
    return cap


def _load_registrants() -> None:
    for name in REGISTRANTS:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - a broken module hides only its own capability
            log.exception("capabilities: could not import %s", name)


def registered() -> list[Capability]:
    """Every registered capability, registrant modules imported first."""
    _load_registrants()
    return list(_registry.values())


def get(permission: str) -> Capability | None:
    _load_registrants()
    return _registry.get(_norm(permission))


def included_ids() -> frozenset[str]:
    """Capabilities that come with Use this agent and have no grant of their own."""
    return frozenset(c.permission for c in registered() if c.default == "included")


# Built-ins enforced by the grant store and the bridge rather than one tool module.
USE_HUB = register(Capability(
    permission="use_hub", label="Use this agent", group="hub",
    description="Chat with this agent and use its unrestricted tools.",
))
MANAGE_ACCESS = register(Capability(
    permission="manage_access", label="Manage access", group="admin",
    description=("Review and change access to this agent and create chat accounts with "
                 "access to it, within your own access. Includes Use this agent, not "
                 "restricted tools. It is not organization administration."),
    delegate_grantable=False,
))


# ---- configuration status (names only, never values) ----------------------------

class _Settings:
    """What this process can see of a hub's settings without fetching a secret:
    the environment before any hub layer, then the hub and restricted files
    (`config_secrets.layer_report`). Values stay inside this object."""

    def __init__(self, hub_dir: Path):
        self.hub_dir = hub_dir
        self._values: dict[str, str] | None = None
        self._unread: list = []

    def _load(self) -> None:
        from dotenv import dotenv_values

        from . import config_secrets as cs

        values = {k: v for k, v in cs._start_env().items()}  # noqa: SLF001 - the deployment layer
        files: dict[str, dict] = {}
        for row in cs.layer_report(self.hub_dir, fetch_secrets=False):
            source = row["source"]
            if source not in files:
                files[source] = dotenv_values(source) if Path(source).is_file() else {}
            values[row["key"]] = files[source].get(row["key"]) or ""
        self._values = values
        self._unread = cs.pointers(self.hub_dir)

    def status(self, cap: Capability) -> tuple[bool | None, str]:
        """(available, short status). available is None when a named secret
        that was not read could hold what is missing."""
        if not cap.requires and not cap.enabled_by:
            return True, ""
        if self._values is None:
            try:
                self._load()
            except Exception:  # noqa: BLE001 - report "not checked", never guess
                log.warning("capabilities: settings for %s could not be inspected", self.hub_dir.name)
                self._values = {}
                self._unread = [None]
        from . import config_secrets as cs

        if cap.enabled_by:
            raw = (self._values.get(cap.enabled_by) or "").strip().lower()
            if raw and raw not in _TRUTHY:
                return False, DISABLED
        absent = [k for k in cap.requires if not (self._values.get(k) or "").strip()]
        if not absent:
            return True, ""
        for pointer in self._unread:
            if pointer is None or any(
                not (pointer.layer == cs.DEPLOYMENT and pointer.filtered and not cs.bridge_deployment_key(k))
                for k in absent
            ):
                return None, NOT_CHECKED
        return False, cap.missing


# ---- the catalogue --------------------------------------------------------------

def _entry(cap: Capability, available: bool | None, status: str) -> dict:
    return dict(
        permission=cap.permission, label=cap.label, description=cap.description,
        sensitive=cap.sensitive, group=cap.group, surfaces=list(cap.surfaces),
        status=status, available=available, default=cap.default,
        delegate_grantable=cap.delegate_grantable, obsolete=False,
    )


def _restricted_names(hub_dir: Path) -> list[str]:
    """Capability names from `restricted/*.py` file names. The modules are
    never imported here, so a broken or hostile file cannot run or break this."""
    from ._fs import resolve_bucket

    restricted = resolve_bucket(hub_dir, "restricted")
    if not restricted:
        return []
    return sorted({_norm(p.stem) for p in restricted.glob("*.py") if not p.name.startswith("_")} - {""})


def _metadata(hub_dir: Path) -> dict:
    import yaml

    from ._fs import resolve_bucket

    identity = resolve_bucket(hub_dir, "identity")
    path = identity / "permissions.yaml" if identity else None
    if not path or not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("identity/permissions.yaml must map capability names to metadata")
    return {_norm(k): v for k, v in data.items()}


_warned: set[tuple[str, str]] = set()


def _connectors(hub_dir: Path, taken: set[str]) -> list[dict]:
    try:
        from . import connect_journey

        rows = connect_journey.permissions(hub_dir) or []
    except Exception:  # noqa: BLE001 - a broken connector list hides connectors only
        log.exception("capabilities: connector list unavailable for %s", hub_dir.name)
        return []
    out = []
    for row in rows:
        pid = _norm(row.get("permission"))
        if not pid or pid in taken:
            continue
        taken.add(pid)
        group = row.get("group") if row.get("group") in GROUPS else "tools"
        out.append(dict(
            permission=pid, label=row.get("label") or _title(pid),
            description=row.get("description") or "", sensitive=bool(row.get("sensitive", True)),
            group=group, surfaces=[s for s in row.get("surfaces") or () if s in SURFACES],
            status=row.get("status") or "", available=row.get("available", True),
            default="grant", delegate_grantable=bool(row.get("delegate_grantable", True)),
            obsolete=False,
        ))
    return out


def _obsolete(permission: str) -> dict:
    return dict(
        permission=permission, label=_title(permission),
        description="This capability no longer exists in this agent. You can remove it, but it can't be granted again.",
        sensitive=False, group=OBSOLETE_GROUP, surfaces=[], status=NO_LONGER_AVAILABLE,
        available=False, default="grant", delegate_grantable=True, obsolete=True,
    )


def catalog(hub_dir: Path, *, granted: Iterable[str] = ()) -> list[dict]:
    """Every capability this hub offers, in display order.

    Registered built-ins, connector capabilities, then the hub's restricted
    capabilities (optional `identity/permissions.yaml` labels them; it cannot
    change a built-in). `granted` ids missing from all of those are added as
    obsolete entries so they stay visible and removable.

    Each entry: permission, label, description, sensitive (the original
    fields) plus group, surfaces, status, available, default,
    delegate_grantable and obsolete. Never contains a setting value."""
    hub_dir = Path(hub_dir)
    settings = _Settings(hub_dir)
    entries: dict[str, dict] = {}
    for cap in registered():
        entries[cap.permission] = _entry(cap, *settings.status(cap))
    builtin = set(entries)
    for row in _connectors(hub_dir, set(entries)):
        entries[row["permission"]] = row
    metadata = _metadata(hub_dir)
    for name in sorted(set(metadata) & builtin):
        if (str(hub_dir), name) not in _warned:
            _warned.add((str(hub_dir), name))
            log.warning("capabilities: identity/permissions.yaml in %s describes the built-in %r; "
                        "that entry is ignored", hub_dir.name, name)
    for name in _restricted_names(hub_dir):
        if name in entries:
            continue  # a restricted module under a built-in id keeps the built-in's row
        meta = metadata.get(name, {})
        if not isinstance(meta, dict):
            raise ValueError(f"Permission metadata for {name} must be a mapping")
        entries[name] = dict(
            permission=name, label=meta.get("label", _title(name)),
            description=meta.get("description", "Use the restricted tools assigned to this capability."),
            sensitive=bool(meta.get("sensitive", False)), group="restricted", surfaces=[],
            status="", available=True, default="grant", delegate_grantable=True, obsolete=False,
        )
    for name in sorted({_norm(g) for g in granted} - set(entries) - {""}):
        entries[name] = _obsolete(name)
    order = {g: i for i, g in enumerate((*GROUPS, OBSOLETE_GROUP))}
    return sorted(entries.values(), key=lambda e: (order.get(e["group"], len(order)), e["permission"]))
