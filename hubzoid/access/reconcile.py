# Hubzoid access management. MIT licensed like the rest of the repository.
"""One-way visibility projection: Casbin `use_hub` -> Open WebUI model visibility.

Casbin is the authority; OWUI's model visibility is a mirror Hubzoid owns, so a
user sees only the hubs they can open. This is the reconciler: it computes, from
the store, which hubs each subject may open, and projects that to OWUI via an
injected `projector(subject, hubs)` (which reuses `gateway_provision`'s OWUI
API). Never bi-directional. `hubzoid access sync` re-runs it — the recovery path.

Enforcement never depends on this: a stray OWUI edit is inert because authz
reads Casbin. The mirror is a gateway-mode convenience (a standalone hub runs
OWUI with model-access bypassed, so there is nothing to project there).
"""
from __future__ import annotations

import logging
from typing import Callable

from .store import ORG, USE_HUB, GrantStore

log = logging.getLogger("hubzoid.access.reconcile")

# A projector applies a subject's visible-hub set to OWUI. Injected so the logic
# is testable and the OWUI coupling stays in one place.
Projector = Callable[[str, list[str]], None]


def visibility_plan(store: GrantStore) -> dict[str, set[str]]:
    """subject -> the set of hubs it may open (`use_hub`), excluding the org
    domain. The wildcard subject '*' means 'everyone signed in' and is left for
    the projector to expand across real OWUI users."""
    plan: dict[str, set[str]] = {}
    for subject, hub, perm in store.list_grants():
        if perm == USE_HUB and hub != ORG:
            plan.setdefault(subject, set()).add(hub)
    return plan


def sync(store: GrantStore, projector: Projector) -> int:
    """Project every subject's visible hubs to OWUI. Returns subjects projected.
    A failed projection is logged and skipped (the row is retried on the next
    sync — the recovery path), never raised."""
    plan = visibility_plan(store)
    done = 0
    for subject, hubs in sorted(plan.items()):
        try:
            projector(subject, sorted(hubs))
            done += 1
        except Exception:  # noqa: BLE001
            log.exception("reconcile: projection failed for %s", subject)
    return done
