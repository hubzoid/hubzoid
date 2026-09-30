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
separately (``gateway --no-bridges``) may not share its environment.
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


def ui_mode(hub_dir: Path | None = None, env: Mapping[str, str] | None = None) -> str:
    """``hubzoid`` or ``openwebui``. Environment first, then the deployment record."""
    raw = _env("HUBZOID_UI", env).lower()
    if not raw:
        raw = str(_manifest(hub_dir, env).get("ui_mode") or "").lower()
    return UI_OPENWEBUI if raw in _LEGACY_NAMES else UI_HUBZOID


def is_legacy(hub_dir: Path | None = None, env: Mapping[str, str] | None = None) -> bool:
    return ui_mode(hub_dir, env) == UI_OPENWEBUI


def auth_enabled(hub_dir: Path | None = None, env: Mapping[str, str] | None = None) -> bool:
    """True when people must sign in. ``HUBZOID_AUTH``, then ``WEBUI_AUTH``, then
    the deployment record. Default off (local single-user mode)."""
    raw = _env("HUBZOID_AUTH", env) or _env("WEBUI_AUTH", env)
    if raw:
        return raw.lower() in _TRUE
    recorded = _manifest(hub_dir, env).get("auth")
    return bool(recorded) if isinstance(recorded, bool) else False


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
