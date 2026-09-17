"""Local deployment discovery shared by gateway, bridges and operator commands.

The gateway owns one manifest; hub pointers contain no credentials. Environment
URLs must agree with registered deployment values. No manifest is required for standalone hubs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _validate_hub_keys(hubs: list[dict]) -> None:
    keys = [h["key"].strip().lower() for h in hubs]
    if len(keys) != len(set(keys)) or any(not key or key == "*" for key in keys):
        raise ValueError(
            "Each hub needs a unique non-reserved access name; rename duplicate hub directories before starting the gateway"
        )


def read(hub_dir: Path, env=None, *, require_hub: bool = True) -> dict:
    env = os.environ if env is None else env
    manifest = env.get("HUBZOID_DEPLOYMENT")
    pointer = Path(hub_dir) / ".hubzoid" / "deployment.json"
    if not manifest and pointer.exists():
        manifest = json.loads(pointer.read_text())["manifest"]
    if not manifest:
        return {}
    data = json.loads(Path(manifest).read_text())
    if data.get("version") != 1:
        raise ValueError(f"Unsupported deployment manifest: {manifest}")
    if require_hub and not any(
        Path(h["path"]).resolve() == Path(hub_dir).resolve()
        for h in data.get("hubs", [])
    ):
        raise ValueError(f"Hub is not registered in deployment manifest: {hub_dir}")
    _validate_hub_keys(data.get("hubs", []))
    return data


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(path)


def save(
    path: Path, *, hubs: list[dict], operational_url: str, owui_url: str, owui_db: str
) -> None:
    _validate_hub_keys(hubs)
    path = path.resolve()
    _write(
        path,
        dict(
            version=1,
            hubs=hubs,
            operational_url=operational_url,
            owui_url=owui_url,
            owui_db=owui_db,
        ),
    )
    for hub in hubs:
        _write(
            Path(hub["path"]) / ".hubzoid" / "deployment.json", {"manifest": str(path)}
        )


def hubs(hub_dir: Path) -> list[dict]:
    return read(hub_dir).get("hubs") or [
        dict(
            key=Path(hub_dir).name.lower(),
            name=Path(hub_dir).name,
            path=str(Path(hub_dir).resolve()),
            model_id=Path(hub_dir).name,
        )
    ]


def hub_path(hub_dir: Path, key: str) -> Path:
    for h in hubs(hub_dir):
        if h["key"] == key:
            return Path(h["path"])
    raise KeyError(key)


def owui_url(hub_dir: Path) -> str:
    configured = read(hub_dir).get("owui_url", "").rstrip("/")
    explicit = os.environ.get("OWUI_INTERNAL_URL", "").rstrip("/")
    if configured and explicit and configured != explicit:
        raise ValueError("OWUI_INTERNAL_URL differs from the registered deployment")
    return configured or explicit or os.environ.get("WEBUI_URL", "").rstrip("/")


def permission_catalog(hub_dir: Path) -> list[dict]:
    """Discover names without executing restricted tool code in the portal.

    Optional identity/permissions.yaml adds labels, descriptions and sensitivity.
    """
    from ._fs import resolve_bucket
    import yaml

    names = {"use_hub", "manage_access"}
    restricted = resolve_bucket(hub_dir, "restricted")
    if restricted:
        names.update(
            p.stem.lower()
            for p in restricted.glob("*.py")
            if not p.name.startswith("_")
        )
    identity = resolve_bucket(hub_dir, "identity")
    meta_path = identity / "permissions.yaml" if identity else None
    metadata = (
        yaml.safe_load(meta_path.read_text()) or {}
        if meta_path and meta_path.exists()
        else {}
    )
    labels = {"use_hub": "Use this agent", "manage_access": "Manage access"}
    out = []
    for name in sorted(names):
        m = metadata.get(name, {})
        if not isinstance(m, dict):
            raise ValueError(f"Permission metadata for {name} must be a mapping")
        out.append(
            dict(
                permission=name,
                label=m.get("label", labels.get(name, name.replace("_", " ").title())),
                description=m.get("description", ""),
                sensitive=bool(m.get("sensitive", False)),
            )
        )
    return out
