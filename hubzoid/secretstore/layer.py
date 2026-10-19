"""The store's layer at bridge start: stored secrets join the environment
below every operator source.

`enable(hub_dir)` runs once, after the bridge has migrated its database and
before it builds its runtime. From then on `config_secrets.apply_layers` asks
`_hook` for the hub's stored values. The hook reads the store once per process
and keeps that snapshot, so a value saved later applies only after a restart
and a serving bridge never changes its environment.

Order inside the store: deployment secrets, then this hub's, so a hub value
wins over a deployment one. A key any operator source sets is left to it and
recorded as overridden. A bound value (an Azure key) whose destination has
changed is left out and recorded as needing to be set again. A value the
deployment key cannot read is left out and recorded as unreadable.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .. import config_secrets as cs
from ..assertions import hub_key
from . import names, store

log = logging.getLogger("hubzoid.secretstore")

IN_USE, OVERRIDDEN, UNREADABLE, REBIND = "in_use", "overridden", "unreadable", "rebind"


@dataclass
class Snapshot:
    plain: dict[str, str] = field(default_factory=dict)
    restricted: dict[str, str] = field(default_factory=dict)
    # (scope, name) -> one of IN_USE, OVERRIDDEN, UNREADABLE, REBIND, and the
    # revision that was read, so a later change reads as needing a restart.
    status: dict[tuple[str, str], str] = field(default_factory=dict)
    revisions: dict[tuple[str, str], int] = field(default_factory=dict)

    def __repr__(self) -> str:  # never print a value by accident
        return f"Snapshot(plain={sorted(self.plain)}, restricted={sorted(self.restricted)})"


_lock = threading.Lock()
_snapshots: dict[str, Snapshot] = {}


def _read(hub_dir: Path, env: Mapping[str, str], operator: frozenset) -> Snapshot:
    snap = Snapshot()
    eng = store.existing_engine(hub_dir)
    if eng is None:
        return snap
    key = hub_key(hub_dir)
    rows = [*store.values(hub_dir, scope="deployment", eng=eng),
            *store.values(hub_dir, scope="hub", target=key, eng=eng)]
    for item in rows:
        meta = item.meta
        status_key = (meta.scope, meta.name)
        if not names.env_name(meta.name):
            continue  # referenced by name only (secret:...), never an environment variable
        snap.revisions[status_key] = meta.revision
        if meta.name in operator:
            snap.status[status_key] = OVERRIDDEN
            continue
        if item.value is None:
            snap.status[status_key] = UNREADABLE
            log.warning("hub %s: the stored secret %s cannot be read with this deployment key. Set it "
                        "again.", hub_dir.name, meta.name)
            continue
        snap.restricted.pop(meta.name, None)
        snap.plain.pop(meta.name, None)
        (snap.restricted if meta.restricted else snap.plain)[meta.name] = item.value
        if meta.scope == "hub" and snap.status.get(("deployment", meta.name)) == IN_USE:
            snap.status[("deployment", meta.name)] = OVERRIDDEN  # this hub stores its own
        snap.status[status_key] = IN_USE
    # Bound values: the destination is decided by the operator layers and the
    # store together, as the runtime will see them.
    effective = {**env, **snap.plain, **snap.restricted}
    for item in rows:
        meta = item.meta
        if meta.destination is None or snap.status.get((meta.scope, meta.name)) != IN_USE:
            continue
        if names.destination(meta.name, effective) != meta.destination:
            snap.plain.pop(meta.name, None)
            snap.restricted.pop(meta.name, None)
            snap.status[(meta.scope, meta.name)] = REBIND
            log.warning("hub %s: %s is stored for a different destination. Set it again for this one.",
                        hub_dir.name, meta.name)
    return snap


def _hook(hub_dir: Path, env: Mapping[str, str], operator: frozenset) -> tuple[dict[str, str], dict[str, str]]:
    path = str(Path(hub_dir).resolve())
    with _lock:
        snap = _snapshots.get(path)
        if snap is None:
            snap = _read(Path(hub_dir), env, operator)
            _snapshots[path] = snap
            if snap.plain or snap.restricted:
                log.info("hub %s: %d stored secret(s) in use", Path(hub_dir).name,
                         len(snap.plain) + len(snap.restricted))
    return dict(snap.plain), dict(snap.restricted)


def enable(hub_dir: Path | None = None) -> None:
    """Turn the store layer on for this process. The next settings load
    applies it."""
    cs.use_store(_hook)


def disable() -> None:
    cs.use_store(None)
    with _lock:
        _snapshots.clear()


def started(hub_dir: Path) -> dict[tuple[str, str], tuple[str, int]]:
    """{(scope, name): (state, revision)} for what the running process did with
    each stored secret at start, or {} when the store layer has not run here."""
    with _lock:
        snap = _snapshots.get(str(Path(hub_dir).resolve()))
    if snap is None:
        return {}
    return {k: (state, snap.revisions.get(k, 0)) for k, state in snap.status.items()}


def active(hub_dir: Path) -> bool:
    """Whether this process loaded its stored secrets (a bridge)."""
    with _lock:
        return str(Path(hub_dir).resolve()) in _snapshots
