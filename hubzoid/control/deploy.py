"""Which deployment a hub belongs to, and where its deployment files live.

A standalone hub is its own deployment: its folder holds the deployment files.
In a gateway, the folder holding the deployment manifest does. A hub can never
override a deployment file (conventions section 6).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .. import deployment


def root(hub_dir: Path) -> Path:
    """The deployment folder for ``hub_dir``."""
    manifest = deployment.manifest_path(Path(hub_dir))
    if manifest is not None:
        return manifest.parent
    return Path(hub_dir)


def deployment_id(hub_dir: Path) -> str:
    """A stable opaque ID for this deployment, the same from every bridge of a
    gateway: derived from the deployment folder's resolved path."""
    folder = str(root(hub_dir).resolve())
    return "dep_" + hashlib.sha256(folder.encode("utf-8")).hexdigest()[:16]


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
