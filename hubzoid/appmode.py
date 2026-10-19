"""How a deployment runs: the Hubzoid web app, and whether it requires sign-in.

Hubzoid 2.0 has one web experience, the Hubzoid web app, which owns accounts,
sign-in, sessions, conversations and personal connections (``--no-ui`` runs
the API alone). Open WebUI is gone; deployments that still need it stay on the
1.x line. Its settings are not silently ignored: ``retired_settings`` names
every one still present, and the ones that decide who may sign in, or which
interface runs, stop a start (``deployment_conflicts``) until they are renamed.

Sign-in is off by default (local, single user on loopback). ``HUBZOID_AUTH``
turns it on.

A gateway records the sign-in in its deployment manifest, because bridges
started separately (``gateway --no-bridges``) may not share its environment.
For a hub registered in a deployment the recorded sign-in wins: a hub ``.env``
or a bridge's own environment can never turn sign-in off behind the gateway's
public edge. ``deployment_conflicts`` names settings that disagree, and a
bridge refuses to start with any. A standalone hub reads its environment.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

UI_HUBZOID = "hubzoid"

_TRUE = {"1", "true", "yes", "on"}
#: Values of HUBZOID_UI (or a recorded ``ui_mode``) that once selected Open WebUI.
_RETIRED_UI_NAMES = {"openwebui", "open-webui", "owui", "legacy"}
UPGRADE_GUIDE = "docs/UPGRADING.md (Hubzoid 2.0)"


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


def ui_mode(hub_dir: Path | None = None, env: Mapping[str, str] | None = None) -> str:  # noqa: ARG001
    """Always ``hubzoid``: the only web experience. Kept for the deployment
    manifest and ``mode_summary``."""
    return UI_HUBZOID


# --- settings Open WebUI used -------------------------------------------------
@dataclass(frozen=True)
class Retired:
    """A setting from the Open WebUI integration. ``fatal`` ones decide who may
    sign in, where people reach the deployment, or which interface runs, so a
    start stops until they are translated; the rest are warnings."""

    name: str
    fatal: bool
    advice: str


def _group(names: str, fatal: bool, advice: str) -> dict[str, Retired]:
    return {n: Retired(n, fatal, advice) for n in names.split()}


#: Every Open WebUI setting with its 2.0 disposition. Present means set at all,
#: blank or false included, so stale configuration never looks supported. Names
#: the web app reads itself (ENABLE_SIGNUP, ENABLE_LOGIN_FORM, ENABLE_OAUTH_SIGNUP,
#: GOOGLE_CLIENT_*, OAUTH_*, OPENAI_API_KEY and OPENAI_API_BASE_URL) are not here.
RETIRED: dict[str, Retired] = {
    **_group("WEBUI_AUTH", True, "Rename it to HUBZOID_AUTH (same value). Sign-in is never turned off "
             "because an old name was dropped."),
    **_group("WEBUI_URL", True, "Rename it to HUBZOID_PUBLIC_URL (same value)."),
    **_group("WEBUI_ADMIN_EMAIL HUBZOID_GATEWAY_ADMIN_EMAIL", True,
             "Rename it to HUBZOID_ADMIN_EMAIL (same value), so the owner is still recognised."),
    **_group("WEBUI_ADMIN_PASSWORD HUBZOID_GATEWAY_ADMIN_PASSWORD", True,
             "Rename it to HUBZOID_ADMIN_PASSWORD, or remove it once the owner has signed in."),
    **_group("DEFAULT_USER_ROLE", False, "New accounts follow the web app's own sign-up rules "
             "(ENABLE_SIGNUP and ENABLE_OAUTH_SIGNUP, docs/auth.md). Remove it."),
    **_group("WEBUI_NAME", False, "Still read as the deployment's name until you set one in "
             "Settings → Brand; then remove it."),
    **_group("WEBUI_SECRET_KEY", False, "Open WebUI's session key is no longer used. Remove it."),
    **_group("HUBZOID_OWUI_DB", False, "Only `hubzoid migrate openwebui` reads it, to find the "
             "Open WebUI database. Remove it once the move is done."),
    **_group("HUBZOID_OWUI_PORT OWUI_INTERNAL_URL HUBZOID_HIDE_OWUI_USERS "
             "HUBZOID_LOCK_OWUI_ACCESS_UI HUBZOID_OWUI_LOCKED_PREFIXES HUBZOID_KEEP_OWUI_SUFFIX "
             "OWUI_NATIVE_MCP WITH_OPENWEBUI HUBZOID_WITH_OPENWEBUI BYPASS_MODEL_ACCESS_CONTROL "
             "HUBZOID_GATEWAY_ALLOW_BYPASS ENABLE_FORWARD_USER_INFO_HEADERS", False,
             "This configured the Open WebUI integration, which 2.0 removes. Remove it."),
    **_group("ENABLE_COMMUNITY_SHARING ENABLE_DIRECT_CONNECTIONS ENABLE_EVALUATION_ARENA_MODELS "
             "ENABLE_NOTES ENABLE_CHANNELS ENABLE_CODE_INTERPRETER ENABLE_IMAGE_GENERATION "
             "ENABLE_RAG_WEB_SEARCH ENABLE_USER_WEBHOOKS ENABLE_TAGS_GENERATION ENABLE_FOLLOW_UP_GENERATION "
             "ENABLE_AUTOCOMPLETE_GENERATION ENABLE_RETRIEVAL_QUERY_GENERATION ENABLE_MEMORY "
             "ENABLE_OLLAMA_API ENABLE_API_KEY ENABLE_API_KEYS USER_PERMISSIONS_FEATURES_API_KEYS "
             "ENABLE_API_KEY_ENDPOINT_RESTRICTIONS ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS "
             "API_KEY_ALLOWED_ENDPOINTS API_KEYS_ALLOWED_ENDPOINTS ENABLE_VERSION_UPDATE_CHECK "
             "SHOW_ADMIN_DETAILS DEFAULT_INTERFACE_SETTINGS ENABLE_PERSISTENT_CONFIG "
             "ENABLE_OAUTH_PERSISTENT_CONFIG USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS "
             "USER_PERMISSIONS_WORKSPACE_TOOLS_ACCESS USER_PERMISSIONS_WORKSPACE_FUNCTIONS_ACCESS "
             "USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS USER_PERMISSIONS_WORKSPACE_PROMPTS_ACCESS "
             "ENABLE_MESSAGE_RATING ENABLE_TITLE_GENERATION ENABLE_ADMIN_CHAT_ACCESS ENABLE_ADMIN_EXPORT "
             "RAG_EMBEDDING_ENGINE BYPASS_EMBEDDING_AND_RETRIEVAL AUDIO_STT_ENGINE RESPONSE_WATERMARK "
             "DEFAULT_PROMPT_SUGGESTIONS DEFAULT_MODELS OPENAI_API_BASE_URLS OPENAI_API_KEYS "
             "OPENAI_API_CONFIGS", False,
             "An Open WebUI feature setting. The Hubzoid web app reads its own chat settings "
             "(Settings → Chat defaults, chat/config.yaml). Remove it."),
}


def retired_settings(hub_dir: Path | None = None, env: Mapping[str, str] | None = None) -> list[Retired]:
    """Open WebUI settings still present: in the environment (every loaded
    layer), as an Open WebUI ``HUBZOID_UI`` value, or as a recorded Open WebUI
    deployment. Fatal ones first."""
    env_map = os.environ if env is None else env
    found = [r for name, r in RETIRED.items() if name in env_map]
    ui = _env("HUBZOID_UI", env)
    if ui and ui.lower() != UI_HUBZOID:
        found.append(Retired("HUBZOID_UI", True,
                             f"HUBZOID_UI={ui}: Hubzoid 2.0 runs only its own web app. Remove HUBZOID_UI, or "
                             "stay on Hubzoid 1.x to keep Open WebUI."
                             if ui.lower() in _RETIRED_UI_NAMES else
                             f"HUBZOID_UI={ui} is not a known interface. Remove HUBZOID_UI."))
    recorded = str(_manifest(hub_dir, env).get("ui_mode") or "")
    if recorded and recorded.lower() != UI_HUBZOID:
        found.append(Retired("ui_mode", True,
                             "The deployment manifest records Open WebUI mode. Move its people and chats "
                             "with `hubzoid migrate openwebui --apply`, then run `hubzoid gateway` with "
                             "Hubzoid 2.0 to rewrite it, or stay on Hubzoid 1.x."))
    return sorted(found, key=lambda r: (not r.fatal, r.name))


def retired_problems(hub_dir: Path | None = None, env: Mapping[str, str] | None = None) -> list[str]:
    """The fatal retired settings, as messages: a start stops on any."""
    return [f"{r.name} is an Open WebUI setting. {r.advice} See {UPGRADE_GUIDE}."
            for r in retired_settings(hub_dir, env) if r.fatal]


def _requested_auth(env: Mapping[str, str] | None) -> tuple[str, str] | None:
    """(name, value) of the sign-in setting the environment asks for
    (``HUBZOID_AUTH``). None when it is not set."""
    raw = _env("HUBZOID_AUTH", env)
    return ("HUBZOID_AUTH", raw) if raw else None


def _recorded_auth(manifest: dict) -> bool | None:
    """The sign-in a deployment recorded, which its bridges keep. None without
    a record."""
    recorded = manifest.get("auth")
    return recorded if isinstance(recorded, bool) else None


def auth_enabled(hub_dir: Path | None = None, env: Mapping[str, str] | None = None) -> bool:
    """True when people must sign in. A hub registered in a web app deployment
    keeps the sign-in its gateway recorded. Otherwise ``HUBZOID_AUTH``. Default
    off (local mode)."""
    recorded = _recorded_auth(_manifest(hub_dir, env))
    if recorded is not None:
        return recorded
    requested = _requested_auth(env)
    return bool(requested) and requested[1].lower() in _TRUE


def deployment_conflicts(hub_dir: Path, env: Mapping[str, str] | None = None) -> list[str]:
    """Reasons this hub must not start: Open WebUI settings that decide sign-in
    or the interface (``retired_problems``), and settings in this environment
    (every configuration layer loaded) that disagree with the deployment this
    hub is registered in. One bridge without sign-in behind the gateway's
    public edge would serve everyone as the local owner."""
    problems = retired_problems(hub_dir, env)
    manifest = _manifest(hub_dir, env)
    if not manifest:
        return problems
    from . import deployment

    where = deployment.manifest_path(Path(hub_dir), env) or "its deployment manifest"
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
    return _env("HUBZOID_PUBLIC_URL", env).rstrip("/")


def allowed_origins(env: Mapping[str, str] | None = None, hub_dir: Path | None = None) -> list[str]:
    """Every origin a browser may use for this deployment: the public URL plus
    ``HUBZOID_ALLOWED_ORIGINS`` (comma separated), then the origins the gateway
    recorded for the deployment this hub is registered in (``hub_dir``, or this
    bridge's ``HUBZOID_HUB_DIR`` when reading the process environment).
    Normalised to ``scheme://host[:port]``. Empty when nothing is configured
    (local mode). Only configured addresses, never one a request carries."""
    out: list[str] = []
    candidates = [public_url(env)] + _env("HUBZOID_ALLOWED_ORIGINS", env).split(",")
    if hub_dir is None and env is None:
        hub_dir = _env("HUBZOID_HUB_DIR") or None
    recorded = _manifest(hub_dir, env).get("allowed_origins") if hub_dir else None
    if isinstance(recorded, list):
        candidates += [str(o) for o in recorded]
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
        "allowed_origins": allowed_origins(env, hub_dir),
    }
