"""Lightweight inbound route names shared by supervisors and bridges."""
from __future__ import annotations
import os
from pathlib import Path

DEFAULT_INBOUND_PORT = 8100


def inbound_port(env=None) -> int:
    env = env if env is not None else os.environ
    try:
        return int((env.get("HUBZOID_INBOUND_PORT") or "").strip() or DEFAULT_INBOUND_PORT)
    except ValueError:
        return DEFAULT_INBOUND_PORT


def _slugify(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in str(text).strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "hub"


def hub_slug(hub_dir, env=None) -> str:
    """The URL slug this hub's webhooks are namespaced under: ``HUBZOID_HUB_SLUG``
    if the operator pinned one, else the slugified folder name.

    The gateway edge and this inbound app must agree on the slug or the route
    404s. Both apply this same rule, so distinct folder names need no config; set
    ``HUBZOID_HUB_SLUG`` only when the gateway had to de-dup a slug collision
    (two hubs with the same folder basename)."""
    env = env if env is not None else os.environ
    pinned = (env.get("HUBZOID_HUB_SLUG") or "").strip()
    if pinned:
        return _slugify(pinned)
    from ..deployment import read
    for registered in read(Path(hub_dir), env).get('hubs', []):
        if Path(registered['path']).resolve() == Path(hub_dir).resolve() and registered.get('slug'):
            return registered['slug']
    return _slugify(Path(hub_dir).name)


