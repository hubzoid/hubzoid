"""Local deployment discovery shared by gateway, bridges and operator commands.

The gateway owns one manifest; hub pointers contain no credentials. Environment
URLs must agree with registered deployment values. No manifest is required for standalone hubs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import datadir


def _validate_hub_keys(hubs: list[dict]) -> None:
    keys = [h["key"].strip().lower() for h in hubs]
    if len(keys) != len(set(keys)) or any(not key or key == "*" for key in keys):
        raise ValueError(
            "Each hub needs a unique non-reserved access name; rename duplicate hub directories before starting the gateway"
        )


def read(hub_dir: Path, env=None, *, require_hub: bool = True) -> dict:
    env = os.environ if env is None else env
    manifest = env.get("HUBZOID_DEPLOYMENT")
    pointer = datadir.state(hub_dir) / "deployment.json"
    if not manifest and pointer.exists():
        manifest = _pointed(pointer)
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


def manifest_path(hub_dir: Path, env=None) -> Path | None:
    """Where this hub's deployment manifest lives (the gateway's data folder
    holds it), or None for a standalone hub."""
    env = os.environ if env is None else env
    manifest = env.get("HUBZOID_DEPLOYMENT")
    pointer = datadir.state(hub_dir) / "deployment.json"
    if not manifest and pointer.exists():
        try:
            manifest = _pointed(pointer)
        except (OSError, ValueError):
            manifest = None
    return Path(manifest) if manifest else None


def _pointed(pointer: Path) -> str | None:
    """The manifest a hub's pointer names. In the deployment's own data folder
    the file is the manifest itself (it has ``hubs``, not ``manifest``)."""
    data = json.loads(pointer.read_text())
    if "manifest" in data:
        return data["manifest"]
    return str(pointer) if "hubs" in data else None


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(path)


def save(
    path: Path, *, hubs: list[dict], operational_url: str, owui_db: str = "",
    owui_database_url: str | None = None, owui_database_schema: str | None = None,
    deployment_secret: dict | None = None, owner: str | None = None,
    public_url: str | None = None, workflow_user: str | None = None,
    ui_mode: str | None = None, auth: bool | None = None,
    allowed_origins: list[str] | None = None, name: str | None = None,
    branding_dir: str | None = None,
) -> None:
    """Write the manifest and each hub's pointer to it.

    `owner` (the configured initial owner's email), `public_url` (the site
    root people open) and `workflow_user` (the gateway's HUBZOID_WORKFLOW_USER)
    let bridges and CLI commands that did not inherit the gateway's environment
    act the same as the gateway. Omitted when unset.

    `owui_db`, `owui_database_url` and `owui_database_schema` say where a
    gateway that ran Open WebUI on 1.x kept its database, for
    `hubzoid migrate openwebui` and the upgrade checks only. Omitted when empty.

    `deployment_secret` ({"name", "region"}) names the gateway's AWS secret so
    external bridges can fetch it. Only the name and region are stored, never a
    value. The key is omitted when there is no deployment secret.

    `ui_mode` ("hubzoid"), `auth` (sign-in on) and
    `allowed_origins` record how the whole deployment runs, so bridges started
    separately (`gateway --no-bridges`) behave like the gateway
    (`hubzoid.appmode`). `name` is the deployment's display name and
    `branding_dir` the folder its page chrome comes from (the web app serves
    it at /branding/*). Each is omitted when None."""
    _validate_hub_keys(hubs)
    path = path.resolve()
    data = dict(
        version=1,
        hubs=hubs,
        operational_url=operational_url,
    )
    for key, value in (("owui_db", owui_db), ("owui_database_url", owui_database_url),
                       ("owui_database_schema", owui_database_schema)):
        if value:
            data[key] = value
    if owner:
        data["owner"] = owner.strip().lower()
    if public_url:
        data["public_url"] = public_url.rstrip("/")
    if workflow_user and workflow_user.strip():
        data["workflow_user"] = workflow_user.strip().lower()
    if ui_mode is not None:
        data["ui_mode"] = str(ui_mode)
    if auth is not None:
        data["auth"] = bool(auth)
    if allowed_origins is not None:
        data["allowed_origins"] = [str(o) for o in allowed_origins]
    if name:
        data["name"] = str(name)
    if branding_dir:
        data["branding_dir"] = str(branding_dir)
    if deployment_secret and deployment_secret.get("name"):
        data["deployment_secret"] = {"name": str(deployment_secret["name"]),
                                     "region": deployment_secret.get("region") or None}
    _write(path, data)
    for hub in hubs:
        _write(
            datadir.state(Path(hub["path"])) / "deployment.json", {"manifest": str(path)}
        )


def deployment_secret(hub_dir: Path, env=None) -> dict | None:
    """The manifest's {"name", "region"} for the deployment secret, if any."""
    pointer = read(hub_dir, env).get("deployment_secret")
    return pointer if isinstance(pointer, dict) and pointer.get("name") else None


def hubs(hub_dir: Path) -> list[dict]:
    registered = read(hub_dir).get("hubs")
    if registered:
        return registered
    from dotenv import dotenv_values
    from .loaders.agents import load_main

    values = dotenv_values(Path(hub_dir) / ".env")
    try:
        name = load_main(hub_dir).spec.name or Path(hub_dir).name
    except (FileNotFoundError, ValueError):
        name = Path(hub_dir).name
    label = values.get("MODEL_LABEL") or os.environ.get("MODEL_LABEL")
    slug = "".join(c if c.isalnum() else "-" for c in name.strip().lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return [dict(key=Path(hub_dir).name.lower(), name=name,
                 path=str(Path(hub_dir).resolve()),
                 model_id=label or slug.strip("-") or "agent")]


def hub_path(hub_dir: Path, key: str) -> Path:
    for h in hubs(hub_dir):
        if h["key"] == key:
            return Path(h["path"])
    raise KeyError(key)


def permission_catalog(hub_dir: Path) -> list[dict]:
    """Every capability this hub offers, from `capabilities.catalog`.

    Restricted names are discovered without executing restricted tool code.
    Optional identity/permissions.yaml labels custom restricted capabilities.
    Each entry keeps the original fields (permission, label, description,
    sensitive) and adds group, surfaces, status, available, default,
    delegate_grantable and obsolete. Built-ins register in the module that
    enforces them (`capabilities.register`), not here.
    """
    from . import capabilities

    return capabilities.catalog(hub_dir)
