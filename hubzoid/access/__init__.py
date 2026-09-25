# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""Access management: per-role tool gating for a hub, enforced in the runtime.

The model is never the gate. A restricted tool lives in `<hub>/restricted/`, its
file name is the permission, and an Open WebUI group of the same name is the key.
The runtime hides tools a caller may not use and fails closed if one is reached
anyway, logging every decision.

Public surface (import from `hubzoid.access`):
  * Identity, current_identity, identity_scope, set_identity, normalize
  * is_allowed                  -> the pure decision
  * apply(hub_dir, registry)    -> guard restricted tools in a tool registry
  * audit                       -> the access log (record / read)

Everything is opt-in: a hub with no `restricted/` folder behaves exactly as
before, so this package is invisible to existing hubs.
"""
from __future__ import annotations

import threading

from . import audit
from . import owui_groups
from .groups import effective_groups
from .guard import apply, guard_tool
from .identity import (
    ANONYMOUS,
    Identity,
    current_identity,
    identity_scope,
    normalize,
    set_identity,
)
from .loader import load_restricted
from .policy import is_allowed
from .store import GrantStore

_stores: "dict[int, GrantStore]" = {}
_stores_lock = threading.Lock()


def store_for(hub_dir) -> GrantStore:
    """The (cached) access store for this deployment's SHARED operational DB.
    Standalone: the hub's own DB. Gateway: the one shared DB (all bridges see the
    same grants + per-hub authority markers), via db.operational_engine."""
    from ..db import operational_engine

    eng = operational_engine(hub_dir)
    key = id(eng)
    gs = _stores.get(key)
    if gs is None:
        with _stores_lock:
            gs = _stores.get(key)
            if gs is None:
                gs = GrantStore(eng)
                _stores[key] = gs
    return gs


__all__ = [
    "ANONYMOUS",
    "GrantStore",
    "Identity",
    "apply",
    "audit",
    "current_identity",
    "effective_groups",
    "guard_tool",
    "identity_scope",
    "is_allowed",
    "load_restricted",
    "normalize",
    "owui_groups",
    "set_identity",
    "store_for",
]
