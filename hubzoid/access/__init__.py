# Hubzoid Enterprise · access management. Production use requires a license
# with the "access" entitlement; free to run for development. See LICENSING.md.
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

from . import audit
from . import owui_groups
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

__all__ = [
    "ANONYMOUS",
    "Identity",
    "apply",
    "audit",
    "current_identity",
    "guard_tool",
    "identity_scope",
    "is_allowed",
    "load_restricted",
    "normalize",
    "owui_groups",
    "set_identity",
]
