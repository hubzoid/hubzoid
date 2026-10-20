"""A deployment setting read from its one home, a file.

``hubzoid.yaml`` holds the deployment's settings in sections (``brand``,
``chat``, ``diagnostics``; ``hubzoid.deployfile``). Until a deployment has
one, the section's older file is read instead for this release
(``branding.yaml``, ``chat/config.yaml``, ``admin.yaml`` in the deployment
folder). There is no copy in the database and no apply step: every bridge
reads the file again when it changes.

An edit that makes the file invalid while it runs changes nothing: the last
good value stays, and ``error`` says since when and why, for Settings,
``hubzoid status`` and doctor. With no good value yet (a start), it raises,
so a policy in the file never falls back to an open default.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import canonical


@dataclass
class Current:
    document: dict
    digest: str
    source: str           # "hubzoid.yaml (chat)", "chat/config.yaml" or "default"
    path: Path | None
    loaded_at: float
    error: str | None = None   # the file is invalid now; this is the last good one


def deployment_file(hub_dir: Path) -> Path | None:
    """The hubzoid.yaml this hub belongs to: HUBZOID_DEPLOYMENT_FILE (set by
    the supervisor), else the one found next to or above the hub."""
    raw = (os.environ.get("HUBZOID_DEPLOYMENT_FILE") or "").strip()
    if raw:
        return Path(raw) if Path(raw).is_file() else None
    from ..deployfile import owning

    return owning(Path(hub_dir))


def settings_root(hub_dir: Path) -> Path:
    """The deployment folder: where hubzoid.yaml is, else where the older
    deployment files are (the hub, or a gateway's data folder)."""
    path = deployment_file(hub_dir)
    if path is not None:
        return path.parent
    from . import deploy

    return deploy.root(Path(hub_dir))


class FileSection:
    def __init__(self, hub_dir: Path, key: str, legacy: str, *, parse: Callable[[Any, Any], dict],
                 default: Callable[[], dict], error: Callable[[str], Exception]):
        self.hub_dir = Path(hub_dir)
        self.key = key
        self.legacy = legacy
        self.parse = parse          # (data, loaded or None) -> validated document; raises on problems
        self.default = default
        self.error_type = error
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self._good: Current | None = None

    def _where(self) -> tuple[Path | None, str, bool]:
        """(file, label, is_section)."""
        dep = deployment_file(self.hub_dir)
        if dep is not None:
            try:
                from ..control import yamlfile

                data = yamlfile.load(dep).data or {}
            except Exception:  # noqa: BLE001 - reported by current()
                return dep, f"hubzoid.yaml ({self.key})", True
            if isinstance(data, dict) and self.key in data:
                return dep, f"hubzoid.yaml ({self.key})", True
        legacy = settings_root(self.hub_dir) / self.legacy
        if legacy.is_file():
            return legacy, self.legacy, False
        return None, "default", False

    def current(self) -> Current:
        path, label, is_section = self._where()
        try:
            st = path.stat() if path is not None else None
            stamp = (str(path), st.st_mtime_ns, st.st_size) if st else (None,)
        except OSError:
            stamp = (str(path), "missing")
        with self._lock:
            if stamp == self._stamp and self._good is not None:
                return self._good
            try:
                doc = self._read(path, is_section)
            except Exception as exc:  # noqa: BLE001 - kept, never loosened
                message = f"{label}: {getattr(exc, 'message', None) or exc}"
                if self._good is None:
                    raise self.error_type(message) from exc
                self._good.error = message
                self._stamp = stamp
                return self._good
            self._good = Current(doc, canonical.digest(doc), label, path, time.time())
            self._stamp = stamp
            return self._good

    def _read(self, path: Path | None, is_section: bool) -> dict:
        if path is None:
            return self.default()
        from . import yamlfile

        loaded = yamlfile.load(path)
        if is_section:
            data = (loaded.data or {}).get(self.key)
            return self.parse(data, None)
        return self.parse(loaded.data, loaded)

    def refused(self) -> Exception:
        """What a write through Settings, the CLI or the API gets."""
        where = "hubzoid.yaml" if deployment_file(self.hub_dir) else self.legacy
        return self.error_type(f"This setting lives in {where}. Edit the file (or ask Claude Code or Codex "
                               "to), and every agent picks it up within a few seconds.")
