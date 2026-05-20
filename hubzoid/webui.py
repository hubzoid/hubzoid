"""Open WebUI subprocess manager.

We start `open-webui serve` as a child process and point it at the hubzoid
bridge as its OpenAI-compatible upstream. Per-hub state (SQLite DB, uploads)
lives under `<hub>/.openwebui-data/` so each hub has isolated history.

`open-webui` is a required dep of hubzoid (`pip install hubzoid` bundles it).
If the binary is not on PATH we tell the user how to repair the install.

Hubzoid sets ~24 env vars on the OWUI subprocess to strip platform surfaces
(community sharing, code interpreter, etc.) so the UI looks like a single
product, not "Open WebUI hosting a model". Every default is applied via
`setdefault` semantics, so a user `.env` value always wins. See
docs/branding.md for the full list and why each is set the way it is.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Default Open WebUI env vars (strip platform surfaces).
# Every entry here is overridable from the operator's .env: we use
# env.setdefault, so anything already set in os.environ (loaded from .env)
# takes precedence. Documented end-to-end in docs/branding.md.
# ---------------------------------------------------------------------------
_OFF = "False"
_ON = "True"

_DEFAULT_OWUI_ENV: dict[str, str] = {
    # --- Strip platform / branding leaks --------------------------------
    "ENABLE_COMMUNITY_SHARING": _OFF,        # "Share to Open WebUI Community" CTA
    "ENABLE_DIRECT_CONNECTIONS": _OFF,       # users plug in their own provider keys
    "ENABLE_EVALUATION_ARENA_MODELS": _OFF,  # multi-model A/B
    "ENABLE_NOTES": _OFF,                    # parallel notes product
    "ENABLE_CHANNELS": _OFF,                 # slack-style channels
    "ENABLE_CODE_INTERPRETER": _OFF,         # bypasses hubzoid's tool model
    "ENABLE_IMAGE_GENERATION": _OFF,         # per-hub opt-in later
    "ENABLE_RAG_WEB_SEARCH": _OFF,           # hubzoid has its own web_search
    "ENABLE_USER_WEBHOOKS": _OFF,
    "ENABLE_TAGS_GENERATION": _OFF,          # extra LLM call, marginal benefit today
    "ENABLE_FOLLOW_UP_GENERATION": _OFF,     # post-reply suggestion chips: extra LLM call per turn + on refresh
    "ENABLE_AUTOCOMPLETE_GENERATION": _OFF,  # input-box autocomplete: extra LLM call on typing pause
    "ENABLE_RETRIEVAL_QUERY_GENERATION": _OFF, # RAG query rewriter: not used (hubzoid doesn't RAG)
    "ENABLE_API_KEY": _OFF,                  # per-user API keys defeat auth
    "ENABLE_VERSION_UPDATE_CHECK": _OFF,     # do not phone home from customer prod
    "ENABLE_MEMORY": _OFF,                   # OWUI's user-memory conflicts with hubzoid memory
    "ENABLE_OLLAMA_API": _OFF,               # we do not proxy ollama
    "SHOW_ADMIN_DETAILS": _OFF,
    "ENABLE_PERSISTENT_CONFIG": _OFF,        # CRITICAL: keep env-vars authoritative
    "ENABLE_OAUTH_PERSISTENT_CONFIG": _OFF,  # same idea for OAuth settings (admin-panel-edited values would otherwise win)

    # --- Workspace permissions: hide tabs from non-admins ---------------
    "USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS": _OFF,
    "USER_PERMISSIONS_WORKSPACE_TOOLS_ACCESS": _OFF,
    "USER_PERMISSIONS_WORKSPACE_FUNCTIONS_ACCESS": _OFF,
    "USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS": _OFF,
    "USER_PERMISSIONS_WORKSPACE_PROMPTS_ACCESS": _OFF,

    # --- Real UX wins, kept on ------------------------------------------
    "ENABLE_MESSAGE_RATING": _ON,            # thumbs up/down
    "ENABLE_TITLE_GENERATION": _ON,          # auto chat titles
    "ENABLE_ADMIN_EXPORT": _ON,              # chat-history export for admins
}


_OWUI_SUFFIX_NEEDLE = "    WEBUI_NAME += ' (Open WebUI)'"
_OWUI_SUFFIX_PATCH = "    pass  # hubzoid: suffix stripped (set HUBZOID_KEEP_OWUI_SUFFIX=True to restore)"


def _patch_owui_suffix(strip: bool) -> None:
    """Patch open_webui/env.py to remove the ' (Open WebUI)' suffix.

    Open WebUI's license permits removing built-in branding for deployments
    under 50 unique end users in any rolling 30-day window, or with an
    enterprise license. Hubzoid's target deployments (Samarth, early Isha)
    are well under that threshold; the patch is enabled by default.

    Operators with deployments that exceed 50 users in a 30-day window
    must set ``HUBZOID_KEEP_OWUI_SUFFIX=True`` in ``.env`` to restore the
    OWUI-mandated branding.

    Idempotent: detects whether the file is already patched and no-ops.
    ``pip install --upgrade open-webui`` reverts the patch; hubzoid
    re-applies on next ``hubzoid run``.
    """
    try:
        import open_webui  # type: ignore
    except ImportError:
        return

    env_py = Path(open_webui.__file__).resolve().parent / "env.py"
    if not env_py.is_file():
        return

    try:
        text = env_py.read_text()
    except OSError:
        return

    if strip:
        if _OWUI_SUFFIX_NEEDLE in text:
            env_py.write_text(text.replace(_OWUI_SUFFIX_NEEDLE, _OWUI_SUFFIX_PATCH))
    else:
        if _OWUI_SUFFIX_PATCH in text:
            env_py.write_text(text.replace(_OWUI_SUFFIX_PATCH, _OWUI_SUFFIX_NEEDLE))


_TRUTHY = {"true", "1", "yes", "on"}
_OAUTH_CLIENT_ID_KEYS = (
    "GOOGLE_CLIENT_ID",
    "MICROSOFT_CLIENT_ID",
    "GITHUB_CLIENT_ID",
    "OAUTH_CLIENT_ID",
)


def _validate_auth_env(env: dict[str, str]) -> None:
    """Refuse to boot if auth is enabled with an unsafe config.

    OWUI silently falls back to a public default `WEBUI_SECRET_KEY` and
    builds OAuth callback URLs from `WEBUI_URL` (default localhost). Both
    are footguns in any real deployment. We catch them at boot.
    """
    if env.get("WEBUI_AUTH", "").strip().lower() not in _TRUTHY:
        return

    secret = env.get("WEBUI_SECRET_KEY", "").strip()
    if not secret or secret == "t0p-s3cr3t":
        raise RuntimeError(
            "WEBUI_AUTH=true requires WEBUI_SECRET_KEY to be set to a random "
            "32+ char string. Generate one with:\n"
            "    openssl rand -hex 32\n"
            "Then add WEBUI_SECRET_KEY=<value> to your hub's .env. "
            "See docs/auth.md."
        )

    has_oauth = any(env.get(k, "").strip() for k in _OAUTH_CLIENT_ID_KEYS)
    if has_oauth and not env.get("WEBUI_URL", "").strip():
        raise RuntimeError(
            "OAuth client IDs are set but WEBUI_URL is not. OAuth callbacks "
            "are built from WEBUI_URL; without it the IdP will redirect to "
            "http://localhost and the sign-in flow will fail. Set "
            "WEBUI_URL=https://your.host in your hub's .env. See docs/auth.md."
        )


def _find_binary() -> str | None:
    """Locate the open-webui executable.

    Order. First, next to the running Python (the common case when hubzoid
    is invoked via its console-script entry point inside a venv). Then PATH.
    """
    sibling = Path(sys.executable).parent / "open-webui"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return shutil.which("open-webui")


def is_available() -> bool:
    return _find_binary() is not None


def start(
    *,
    hub_dir: Path,
    bridge_port: int,
    ui_port: int,
    api_key: str,
    model_label: str,
    webui_name: str | None,
    suggestions: list[str] | None = None,
    response_watermark: str | None = None,
) -> subprocess.Popen:
    """Spawn Open WebUI as a subprocess. Returns the Popen handle.

    `suggestions` populates the new-chat quick-start buttons. Sourced from
    the main agent's AGENTS.md frontmatter (`suggestions:` field).
    `response_watermark` defaults to the hub folder name when None.
    """
    # Strip the OWUI "(Open WebUI)" suffix from WEBUI_NAME before launching
    # the subprocess. License-permitted for deployments <50 users / 30 days.
    # Operator can opt out by setting HUBZOID_KEEP_OWUI_SUFFIX=True.
    keep_suffix = os.environ.get("HUBZOID_KEEP_OWUI_SUFFIX", "").lower() in ("true", "1", "yes")
    _patch_owui_suffix(strip=not keep_suffix)

    binary = _find_binary()
    if binary is None:
        raise FileNotFoundError(
            "open-webui not found next to the running Python or on PATH. "
            "It is bundled with hubzoid; reinstall to repair:\n"
            "    pip install --force-reinstall hubzoid\n"
            "or install it directly:\n"
            "    pip install open-webui"
        )

    data_dir = hub_dir / ".openwebui-data"
    data_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()

    # 1. Wiring + per-hub state. These are not operator-overridable; they
    # are how hubzoid joins the bridge to OWUI.
    env["DATA_DIR"] = str(data_dir)
    env["OPENAI_API_BASE_URL"] = f"http://127.0.0.1:{bridge_port}/v1"
    env["OPENAI_API_KEY"] = api_key
    env["DEFAULT_MODELS"] = model_label

    # 2. Auth default off for local dev. Operator overrides via .env.
    env.setdefault("WEBUI_AUTH", "False")

    # 3. Branding. WEBUI_NAME cascades via the cli's resolver; we just pass
    # the resolved value. RESPONSE_WATERMARK defaults to the hub folder
    # name silently (not surfaced in .env; rarely changed).
    if webui_name:
        env.setdefault("WEBUI_NAME", webui_name)
    env.setdefault("RESPONSE_WATERMARK", response_watermark or hub_dir.name)

    # 4. Quick-start prompt suggestions, from AGENTS.md frontmatter.
    # OWUI expects a JSON array of objects with `content` keys.
    if suggestions:
        payload = [{"content": s} for s in suggestions if s]
        env.setdefault("DEFAULT_PROMPT_SUGGESTIONS", json.dumps(payload))

    # 5. The big strip. Apply hubzoid defaults; operator .env wins.
    for key, value in _DEFAULT_OWUI_ENV.items():
        env.setdefault(key, value)

    # 6. Refuse to boot if auth is on with an unsafe config.
    _validate_auth_env(env)

    log_path = data_dir / "openwebui.log"
    log_file = log_path.open("ab", buffering=0)
    cmd = [binary, "serve", "--host", "127.0.0.1", "--port", str(ui_port)]
    proc = subprocess.Popen(cmd, env=env, stdout=log_file, stderr=subprocess.STDOUT)
    proc._log_path = log_path  # type: ignore[attr-defined]
    return proc
