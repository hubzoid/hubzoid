# Hubzoid Enterprise · access management. Production use requires a license
# with the "access" entitlement; free to run for development. See LICENSING.md.
"""Seed per-hub identity + access into the shared gateway Open WebUI.

A gateway fronts N hubs through ONE Open WebUI, so per-hub identity cannot
come from OWUI's global env vars (DEFAULT_PROMPT_SUGGESTIONS is one list, the
chrome logo is one file). It lives on each hub's OWUI *model entry* instead:
picker name, description, quick-start suggestion prompts, avatar — plus a
per-team group holding read access, so "who sees which hub" is enforced by
OWUI's model ACL (BYPASS_MODEL_ACCESS_CONTROL stays off in gateways).

This module drives OWUI's REST API — deliberately NOT its SQLite DB. The API
is the version-stable contract; the DB is not (0.9.x replaced the
`model.access_control` column with an `access_grant` table), and writing a
live SQLite file OWUI holds open risks lock contention. Endpoints used:

    POST /api/v1/auths/signin        admin login -> bearer token
    POST /api/v1/auths/signup        first-user bootstrap (OWUI auto-promotes
                                     the first account to admin)
    GET  /api/v1/groups/             existing groups
    POST /api/v1/groups/create       per-hub team group
    GET  /api/v1/models/model?id=    existence probe (404 = not provisioned)
    POST /api/v1/models/create       first boot: model row + group read grant
    POST /api/v1/models/model/update later boots: refresh identity fields

Overwrite policy (the part admins care about):
  * hubzoid owns the hub's *identity* fields — name, description,
    suggestion_prompts, avatar — and refreshes them every boot (the hub's
    AGENTS.md is the source of truth).
  * hubzoid NEVER touches access after creation: updates omit
    `access_grants`, which OWUI treats as "leave grants alone". Admin ACL
    edits in the Workspace UI survive re-boots.
  * other meta keys the admin set (capabilities, tags, ...) are merged, not
    replaced.

Fail-safe: one hub failing skips that hub and continues; only unusable admin
credentials raise (ProvisionError), and the CLI catches even that — a
provisioning problem must never take the gateway down.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("hubzoid.gateway")

# Raster formats OWUI's profile-image validator accepts as data URIs (it
# rejects SVG, which can carry scripts).
_AVATAR_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class ProvisionError(RuntimeError):
    """Admin auth failed — nothing could be provisioned."""


@dataclass(frozen=True)
class HubSpec:
    """One hub's identity, as the gateway plan captured it."""

    model_id: str                       # OWUI model id == the bridge's label
    name: str                           # picker display name
    group: str                          # team group name (the hub slug)
    suggestions: tuple[str, ...] = ()
    description: str = ""
    logo: Path | None = None


def provision(
    *,
    base_url: str,
    email: str,
    password: str,
    hubs: list[HubSpec],
    allow_bootstrap: bool = False,
    client: httpx.Client | None = None,
) -> list[str]:
    """Provision every hub's model entry + team group. Returns action lines
    for the console (one per hub: created / refreshed / skipped).

    `allow_bootstrap` should be True only on a FRESH data dir (no webui.db
    before this boot): it permits the first-user signup that OWUI promotes to
    admin. On an established gateway it stays False so a wrong password fails
    loudly instead of quietly signing up a stray account.
    """
    own_client = client is None
    if client is None:
        client = httpx.Client(base_url=base_url, timeout=15.0)
    try:
        token = _signin_or_bootstrap(client, email, password, allow_bootstrap)
        headers = {"Authorization": f"Bearer {token}"}
        groups = _group_ids(client, headers)

        actions: list[str] = []
        for hub in hubs:
            try:
                actions.append(_provision_hub(client, headers, groups, hub))
            except Exception as exc:  # noqa: BLE001 — one hub never blocks the rest
                log.warning("provision: hub %s failed: %s", hub.model_id, exc)
                actions.append(f"{hub.model_id}: skipped ({exc})")
        return actions
    finally:
        if own_client:
            client.close()


def _signin_or_bootstrap(client: httpx.Client, email: str, password: str,
                         allow_bootstrap: bool) -> str:
    """Admin token via signin; only on a fresh data dir (allow_bootstrap) fall
    back to first-user signup, which OWUI auto-promotes to admin. On an
    established gateway a signin failure is a credential problem — surfaced,
    never papered over with a signup that would create a stray account."""
    signin = client.post("/api/v1/auths/signin", json={"email": email, "password": password})
    if signin.status_code == 200:
        return signin.json()["token"]
    if allow_bootstrap:
        signup = client.post(
            "/api/v1/auths/signup",
            json={"name": "Hubzoid Admin", "email": email, "password": password},
        )
        if signup.status_code == 200:
            log.info("provision: bootstrapped first admin %s", email)
            return signup.json()["token"]
    raise ProvisionError(
        f"cannot sign in to Open WebUI as {email} (signin HTTP {signin.status_code}); "
        "check HUBZOID_GATEWAY_ADMIN_EMAIL/PASSWORD"
    )


def _group_ids(client: httpx.Client, headers: dict) -> dict[str, str]:
    """Existing groups as {lowercased name: id}."""
    r = client.get("/api/v1/groups/", headers=headers)
    r.raise_for_status()
    return {g["name"].strip().lower(): g["id"] for g in r.json()}


def _ensure_group(client: httpx.Client, headers: dict, groups: dict[str, str],
                  hub: HubSpec) -> str:
    key = hub.group.strip().lower()
    if key in groups:
        return groups[key]
    r = client.post(
        "/api/v1/groups/create",
        headers=headers,
        json={
            "name": hub.group,
            "description": f"Team access for the {hub.name} hub (created by hubzoid gateway).",
        },
    )
    r.raise_for_status()
    gid = r.json()["id"]
    groups[key] = gid
    return gid


def _identity_meta(hub: HubSpec) -> dict:
    """The meta fields hubzoid owns. Empty fields are omitted entirely so
    OWUI's own defaults apply (never an empty key shadowing them)."""
    meta: dict = {}
    if hub.description:
        meta["description"] = hub.description
    if hub.suggestions:
        meta["suggestion_prompts"] = [{"content": s} for s in hub.suggestions if s]
    avatar = _avatar_data_uri(hub.logo)
    if avatar:
        meta["profile_image_url"] = avatar
    return meta


def _avatar_data_uri(logo: Path | None) -> str | None:
    if logo is None:
        return None
    mime = _AVATAR_MIME.get(logo.suffix.lower())
    if mime is None:
        return None  # e.g. SVG — OWUI's validator rejects it
    try:
        return f"data:{mime};base64," + base64.b64encode(logo.read_bytes()).decode()
    except OSError as exc:
        log.warning("provision: cannot read logo %s: %s", logo, exc)
        return None


def _provision_hub(client: httpx.Client, headers: dict, groups: dict[str, str],
                   hub: HubSpec) -> str:
    gid = _ensure_group(client, headers, groups, hub)

    # Existence probe. Strictly 200 = exists, 404 = missing; anything else
    # (500 while the DB warms, an auth blip, a proxy 502) raises and skips
    # this hub for THIS boot — misreading an error as "missing" would take
    # the create path, whose access_grants could clobber an admin's ACL.
    r = client.get("/api/v1/models/model", params={"id": hub.model_id}, headers=headers)
    if r.status_code == 200:
        existing = r.json()
    elif r.status_code == 404:
        existing = None
    else:
        r.raise_for_status()

    if existing is None:
        # First boot for this hub: model row + private ACL (its group reads).
        form = {
            "id": hub.model_id,
            "base_model_id": None,
            "name": hub.name,
            "meta": _identity_meta(hub),
            "params": {},
            "access_grants": [
                {"principal_type": "group", "principal_id": gid, "permission": "read"},
            ],
            "is_active": True,
        }
        client.post("/api/v1/models/create", headers=headers, json=form).raise_for_status()
        return f"{hub.model_id}: created (group '{hub.group}' has read access)"

    # Later boots: refresh identity, merge admin meta, NEVER send
    # access_grants (None = OWUI leaves the grants untouched). Identity keys
    # are cleared before merging so a field REMOVED from AGENTS.md (or a
    # deleted logo) also disappears here — the hub is the source of truth for
    # its identity; everything else in meta stays the admin's.
    merged_meta = dict(existing.get("meta") or {})
    for key in ("description", "suggestion_prompts", "profile_image_url"):
        merged_meta.pop(key, None)
    merged_meta.update(_identity_meta(hub))
    form = {
        "id": hub.model_id,
        "base_model_id": existing.get("base_model_id"),
        "name": hub.name,
        "meta": merged_meta,
        "params": existing.get("params") or {},
        "access_grants": None,
        "is_active": existing.get("is_active", True),
    }
    client.post("/api/v1/models/model/update", headers=headers, json=form).raise_for_status()
    return f"{hub.model_id}: identity refreshed (access left as-is)"
