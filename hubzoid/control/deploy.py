"""Which deployment a hub belongs to, and where its deployment files live.

A standalone hub is its own deployment: its folder holds the deployment files.
In a gateway, the folder holding the deployment manifest does. A hub can never
override a deployment file (conventions section 6).
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from .. import deployment

log = logging.getLogger("hubzoid.control")


def root(hub_dir: Path) -> Path:
    """The deployment folder for ``hub_dir``."""
    manifest = deployment.manifest_path(Path(hub_dir))
    if manifest is not None:
        return manifest.parent
    return Path(hub_dir)


_ids: dict[tuple, str] = {}


def _path_id(hub_dir: Path) -> str:
    folder = str(root(hub_dir).resolve())
    return "dep_" + hashlib.sha256(folder.encode("utf-8")).hexdigest()[:16]


def deployment_id(hub_dir: Path) -> str:
    """A stable opaque ID for this deployment, the same from every bridge of a
    gateway. It is stored in the shared operational store the first time it is
    needed (``hubzoid.auth.settings``), with the value it has always had (a
    hash of the deployment folder's path), so a restore or a different mount
    path keeps it. When the store can't be read, the path value is used."""
    folder = str(root(hub_dir).resolve())
    key = (folder, os.environ.get("HUBZOID_OPERATIONAL_DB"), os.environ.get("DATABASE_URL"))
    found = _ids.get(key)
    if found:
        return found
    computed = _path_id(hub_dir)
    try:
        from ..auth import settings as auth_settings

        found = auth_settings.setdefault(Path(hub_dir), "deployment_id", computed)
    except Exception:  # noqa: BLE001 - the id must never break a request
        log.warning("deploy: stored deployment id unreadable; using the path value", exc_info=True)
        return computed
    _ids[key] = found
    return found


def resolve_inside(base: Path, relative: str) -> Path:
    """``relative`` resolved under ``base``. Refuses absolute paths, ``..`` and
    symlinks that escape. Raises ``ValueError``."""
    if not relative or relative.startswith(("/", "\\")) or ":" in relative.split("/")[0]:
        raise ValueError("Use a path relative to the deployment folder.")
    parts = Path(relative).parts
    if any(p == ".." for p in parts):
        raise ValueError("Paths can't use '..'.")
    base = Path(base).resolve()
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise ValueError("That path leaves the deployment folder.")
    return target
