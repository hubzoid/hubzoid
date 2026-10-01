"""Which web experience a deployment runs, and whether it requires sign-in.

Two UI modes:

  * ``hubzoid`` (default): the Hubzoid web app. Hubzoid owns accounts, sign-in,
    sessions, conversations and personal connections. No Open WebUI.
  * ``openwebui`` (legacy, one release): the bundled Open WebUI chat and
    accounts, exactly as in Hubzoid 1.0.x. Needs ``pip install
    "hubzoid[openwebui]"`` and ``HUBZOID_UI=openwebui``.

Sign-in is off by default (local, single user on loopback). ``HUBZOID_AUTH``
turns it on; the 1.0.x name ``WEBUI_AUTH`` is still honoured so existing hub
``.env`` files keep their meaning after an upgrade.

A gateway records both facts in its deployment manifest, because bridges started
separately (``gateway --no-bridges``) may not share its environment. For a hub
registered in a deployment the recorded mode wins, and so does the recorded
sign-in of a web app deployment: a hub ``.env`` or a bridge's own environment
can never turn sign-in off behind the gateway's public edge.
``deployment_conflicts`` names settings that disagree, and a bridge refuses to
start with any. A standalone hub reads its environment as before, and so does
the sign-in of a legacy (Open WebUI) deployment, which Open WebUI enforces.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

UI_HUBZOID = "hubzoid"
UI_OPENWEBUI = "openwebui"

_TRUE = {"1", "true", "yes", "on"}
_LEGACY_NAMES = {"openwebui", "open-webui", "owui", "legacy"}


def _env(name: str, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get(name) or "").strip()


def _manifest(hub_dir: Path | None, env: Mapping[str, str] | None) -> dict:
    if hub_dir is None:
        return {}
    try:
        from . import deployment

        return deployment.read(Path(hub_dir), env) or {}
    except Exception:  # noqa: BLE001 — an unreadable manifest means "no record"
        return {}


def _mode(raw: str) -> str:
    return UI_OPENWEBUI if raw.strip().lower() in _LEGACY_NAMES else UI_HUBZOID


def ui_mode(hub_dir: Path | None = None, env: Mapping[str, str] | None = None) -> str:
    """``hubzoid`` or ``openwebui``. The mode recorded for the deployment this
    hub is registered in, else ``HUBZOID_UI``."""
    recorded = str(_manifest(hub_dir, env).get("ui_mode") or "")
    return _mode(recorded or _env("HUBZOID_UI", env))


def is_legacy(hub_dir: Path | None = None, env: Mapping[str, str] | None = None) -> bool:
    return ui_mode(hub_dir, env) == UI_OPENWEBUI


def _requested_auth(env: Mapping[str, str] | None) -> tuple[str, str] | None:
    """(name, value) of the sign-in setting the environment asks for:
    ``HUBZOID_AUTH``, then ``WEBUI_AUTH``. None when neither is set."""
    for name in ("HUBZOID_AUTH", "WEBUI_AUTH"):
        raw = _env(name, env)
        if raw:
            return name, raw
    return None


def _recorded_auth(manifest: dict) -> bool | None:
    """The sign-in a web app deployment recorded, which its bridges keep. None
    without a record, or for a legacy deployment (Open WebUI signs people in)."""
    recorded = manifest.get("auth")
    mode = str(manifest.get("ui_mode") or "")
    if not isinstance(recorded, bool) or not mode or _mode(mode) != UI_HUBZOID:
        return None
    return recorded


def auth_enabled(hub_dir: Path | None = None, env: Mapping[str, str] | None = None) -> bool:
    """True when people must sign in. A hub registered in a web app deployment
    keeps the sign-in its gateway recorded. Otherwise ``HUBZOID_AUTH``, then
    ``WEBUI_AUTH``, then the deployment record. Default off (local mode)."""
    manifest = _manifest(hub_dir, env)
    recorded = _recorded_auth(manifest)
    if recorded is not None:
        return recorded
    requested = _requested_auth(env)
    if requested:
        return requested[1].lower() in _TRUE
    recorded = manifest.get("auth")
    return bool(recorded) if isinstance(recorded, bool) else False


def deployment_conflicts(hub_dir: Path, env: Mapping[str, str] | None = None) -> list[str]:
    """Settings in this environment (every configuration layer loaded) that
    disagree with the deployment this hub is registered in. A bridge with any
    must not start: one bridge in another mode, or without sign-in behind the
    gateway's public edge, would serve everyone as the local owner. Empty for a
    standalone hub."""
    manifest = _manifest(hub_dir, env)
    if not manifest:
        return []
    from . import deployment

    where = deployment.manifest_path(Path(hub_dir), env) or "its deployment manifest"
    problems: list[str] = []
    recorded_mode = str(manifest.get("ui_mode") or "")
    asked_mode = _env("HUBZOID_UI", env)
    if recorded_mode and asked_mode and _mode(asked_mode) != _mode(recorded_mode):
        problems.append(
            f"HUBZOID_UI={asked_mode} disagrees with the deployment this hub belongs to ({where}), "
            f"which runs {'Open WebUI' if _mode(recorded_mode) == UI_OPENWEBUI else 'the Hubzoid web app'}. "
            "Remove HUBZOID_UI from this hub's .env and this bridge's environment: the mode is set "
            "once, in the gateway's environment.")
    recorded = _recorded_auth(manifest)
    requested = _requested_auth(env)
    if recorded is not None and requested and (requested[1].lower() in _TRUE) != recorded:
        name, raw = requested
        problems.append(
            f"{name}={raw} disagrees with the deployment this hub belongs to ({where}), which runs "
            f"with sign-in {'on' if recorded else 'off'}. Remove {name} from this hub's .env and this "
            "bridge's environment: sign-in is set once, in the gateway's environment.")
    return problems


def public_url(env: Mapping[str, str] | None = None) -> str:
    """The primary public origin (no trailing slash), or '' when not configured."""
    raw = _env("HUBZOID_PUBLIC_URL", env) or _env("WEBUI_URL", env)
    return raw.rstrip("/")


def allowed_origins(env: Mapping[str, str] | None = None) -> list[str]:
    """Every origin a browser may use for this deployment: the public URL plus
    ``HUBZOID_ALLOWED_ORIGINS`` (comma separated). Normalised to
    ``scheme://host[:port]``. Empty when nothing is configured (local mode)."""
    out: list[str] = []
    candidates = [public_url(env)] + _env("HUBZOID_ALLOWED_ORIGINS", env).split(",")
    for raw in candidates:
        origin = normalize_origin(raw)
        if origin and origin not in out:
            out.append(origin)
    return out


def normalize_origin(raw: str | None) -> str:
    parsed = urlparse((raw or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    port = parsed.port
    default = 443 if parsed.scheme == "https" else 80
    netloc = host if port in (None, default) else f"{host}:{port}"
    if ":" in host and not host.startswith("["):  # IPv6 literal
        netloc = f"[{host}]" if port in (None, default) else f"[{host}]:{port}"
    return f"{parsed.scheme}://{netloc}"


def is_loopback_host(host: str | None) -> bool:
    """True for 127.0.0.0/8, ::1 and ``localhost``. ``0.0.0.0`` is NOT loopback."""
    h = (host or "").strip().strip("[]").lower()
    if h in ("localhost", ""):
        return h == "localhost"
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def mode_summary(hub_dir: Path | None = None, env: Mapping[str, str] | None = None) -> dict:
    """Small, secret-free description used by doctor, the deployment manifest
    and the web app's session endpoint."""
    return {
        "ui_mode": ui_mode(hub_dir, env),
        "auth": auth_enabled(hub_dir, env),
        "public_url": public_url(env),
        "allowed_origins": allowed_origins(env),
    }
