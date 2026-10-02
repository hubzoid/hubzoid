# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
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


def sync_owui(hub_dir) -> dict:
    """Replace the visibility of every registered hub model via the OWUI API.

    Recompute the complete ACL, including empty sets: a user's last revocation
    must remove visibility. Public entry is expanded over current OWUI accounts;
    the periodic sync includes newly signed-up users and excludes blocked users.
    """
    import time
    from .. import deployment
    from ..appmode import is_legacy
    from . import store_for
    from .owui import client_for, users

    if not is_legacy(hub_dir):
        # The web app mode has no Open WebUI picker to mirror: each request is
        # decided by the bridge and /api/agents.
        return {"state": "direct"}
    store = store_for(hub_dir)
    targets = deployment.hubs(hub_dir)
    result = {"state": "ok", "models": 0, "updated": time.time()}
    try:
        with client_for(hub_dir) as c:
            people = users(c)
            store.reconcile_accounts(people)
            for h in targets:
                r = c.get("/api/v1/models/model", params={"id": h["model_id"]})
                r.raise_for_status()
                old = r.json()
                grants = [
                    dict(principal_type="user", principal_id=u["id"], permission="read")
                    for u in people
                    if u.get("role") != "pending"
                    and store.can(u["email"], h["key"], USE_HUB)
                ]
                form = {
                    k: old[k]
                    for k in (
                        "id",
                        "name",
                        "base_model_id",
                        "params",
                        "meta",
                        "is_active",
                    )
                    if k in old
                }
                form["access_grants"] = grants
                c.post("/api/v1/models/model/update", json=form).raise_for_status()
                result["models"] += 1
    except Exception as exc:
        log.exception("OWUI visibility synchronization failed")
        result.update(
            state="error",
            error=f"{type(exc).__name__}: visibility sync failed; check service credentials and server logs",
        )
    store.set_metadata("visibility_sync", result)
    return result


def sync_status(hub_dir) -> dict:
    from . import store_for

    from .. import deployment
    from ..appmode import is_legacy

    if not deployment.read(hub_dir) or not is_legacy(hub_dir):
        return {"state": "direct"}   # no Open WebUI picker to mirror
    gs = store_for(hub_dir)
    return gs.metadata("visibility_sync", {"state": "not-run"})
