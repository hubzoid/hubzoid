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
    path: Path, *, hubs: list[dict], operational_url: str, owui_url: str, owui_db: str,
    owui_database_url: str | None = None, owui_database_schema: str | None = None,
    deployment_secret: dict | None = None, owner: str | None = None,
    public_url: str | None = None, workflow_user: str | None = None,
    hide_owui_users: bool | None = None, sign_in: dict | None = None,
) -> None:
    """Write the manifest and each hub's pointer to it.

    `owner` (the configured initial owner's email), `public_url` (the site
    root people open) and `workflow_user` (the gateway's HUBZOID_WORKFLOW_USER)
    let bridges and CLI commands that did not inherit the gateway's environment
    act the same as the gateway. Omitted when unset.

    `sign_in` (from `sign_in_flags`) records how the chat app signs people in,
    as booleans and a domain list only: never a client id or secret.

    `deployment_secret` ({"name", "region"}) names the gateway's AWS secret so
    external bridges can fetch it. Only the name and region are stored, never a
    value. The key is omitted when there is no deployment secret."""
    _validate_hub_keys(hubs)
    path = path.resolve()
    data = dict(
        version=1,
        hubs=hubs,
        operational_url=operational_url,
        owui_url=owui_url,
        owui_db=owui_db,
        owui_database_url=owui_database_url,
        owui_database_schema=owui_database_schema,
    )
    if owner:
        data["owner"] = owner.strip().lower()
    if public_url:
        data["public_url"] = public_url.rstrip("/")
    if workflow_user and workflow_user.strip():
        data["workflow_user"] = workflow_user.strip().lower()
    if hide_owui_users is not None:
        # The deployment's default for hiding Open WebUI's user management
        # (an explicit HUBZOID_HIDE_OWUI_USERS still wins at the edge).
        data["hide_owui_users"] = bool(hide_owui_users)
    if sign_in is not None:
        data["sign_in"] = _flags_only(sign_in)
    if deployment_secret and deployment_secret.get("name"):
        data["deployment_secret"] = {"name": str(deployment_secret["name"]),
                                     "region": deployment_secret.get("region") or None}
    _write(path, data)
    for hub in hubs:
        _write(
            Path(hub["path"]) / ".hubzoid" / "deployment.json", {"manifest": str(path)}
        )


def hide_owui_users_default(prior: dict, *, fresh: bool, env) -> bool | None:
    """The deployment's recorded default for hiding Open WebUI's user
    management. Kept once recorded. Recorded True only for a new deployment (no
    earlier manifest, no chat-app database yet) set up with Console accounts:
    sign-in on and the service account configured. None (nothing recorded, the
    Users page stays) for every existing deployment."""
    if prior.get("hide_owui_users") is not None:
        return bool(prior["hide_owui_users"])
    on = lambda k: (env.get(k) or "").strip().lower() in ("1", "true", "yes", "on")  # noqa: E731
    if (fresh and not prior and on("WEBUI_AUTH")
            and (env.get("HUBZOID_GATEWAY_ADMIN_EMAIL") or "").strip()
            and (env.get("HUBZOID_GATEWAY_ADMIN_PASSWORD") or "").strip()):
        return True
    return None


def sign_in_flags(env) -> dict:
    """Non-secret facts about the chat app's sign-in setup, parsed exactly as
    Open WebUI 0.11.4 parses them (`config.py`), for the Console's Add user.

    * "google": GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are both set (Open
      WebUI registers Google only then).
    * "merge_by_email": OAUTH_MERGE_ACCOUNTS_BY_EMAIL is true, so a Google
      sign-in attaches to the existing account with that email.
    * "allowed_domains": present only when OAUTH_ALLOWED_DOMAINS restricts
      Google sign-in (the values, compared exactly as Open WebUI does).
    * "oauth_settings_in_app": ENABLE_OAUTH_PERSISTENT_CONFIG is true, so the
      live OAuth settings may have been changed inside the chat app and cannot
      be read from here.

    Never records a client id, secret or any other value."""
    def true(key: str, default: str = "False") -> bool:
        return (env.get(key) or default).lower() == "true"

    flags: dict = {
        "google": bool(env.get("GOOGLE_CLIENT_ID") and env.get("GOOGLE_CLIENT_SECRET")),
        "merge_by_email": true("OAUTH_MERGE_ACCOUNTS_BY_EMAIL"),
    }
    raw = env.get("OAUTH_ALLOWED_DOMAINS")
    domains = [d.strip() for d in ("*" if raw is None else raw).split(",")]
    if "*" not in domains:
        flags["allowed_domains"] = domains
    if true("ENABLE_OAUTH_PERSISTENT_CONFIG"):
        flags["oauth_settings_in_app"] = True
    return flags


def _flags_only(sign_in: dict) -> dict:
    out = {k: bool(sign_in.get(k)) for k in ("google", "merge_by_email")}
    if isinstance(sign_in.get("allowed_domains"), list):
        out["allowed_domains"] = [str(d) for d in sign_in["allowed_domains"]]
    if sign_in.get("oauth_settings_in_app"):
        out["oauth_settings_in_app"] = True
    return out


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


def owui_url(hub_dir: Path) -> str:
    configured = read(hub_dir).get("owui_url", "").rstrip("/")
    explicit = os.environ.get("OWUI_INTERNAL_URL", "").rstrip("/")
    if configured and explicit and configured != explicit:
        raise ValueError("OWUI_INTERNAL_URL differs from the registered deployment")
    return configured or explicit or os.environ.get("WEBUI_URL", "").rstrip("/")


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
