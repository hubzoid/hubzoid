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
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import branding

log = logging.getLogger("hubzoid.webui")

# ---------------------------------------------------------------------------
# Default Open WebUI env vars (strip platform surfaces).
# Every entry here is overridable from the operator's .env: we use
# env.setdefault, so anything already set in os.environ (loaded from .env)
# takes precedence. Documented end-to-end in docs/branding.md.
# ---------------------------------------------------------------------------
_OFF = "False"
_ON = "True"

# One operator switch for OWUI-native per-user MCP. `OWUI_NATIVE_MCP=true` in a
# hub's .env expands to the OWUI flags the "+ -> Integrations -> Tools" OAuth
# flow needs: persist config to webui.db (so admin-registered MCP servers land
# where the bridge reads them - OWUI keeps that config in memory otherwise) plus
# the tools permissions hubzoid strips by default. Opt-in so existing hubs stay
# env-authoritative and reproducible; the bridge injection (owui_mcp) reads the
# same flag. See docs/mcp.md and docs/per-user-tool-connections.html.
_OWUI_NATIVE_MCP_ENV = {
    "ENABLE_PERSISTENT_CONFIG": _ON,
    "USER_PERMISSIONS_WORKSPACE_TOOLS_ACCESS": _ON,
    "USER_PERMISSIONS_FEATURES_DIRECT_TOOL_SERVERS": _ON,
    "ENABLE_DIRECT_CONNECTIONS": _ON,
}


def _seed_owui_config_once(data_dir: Path) -> None:
    """Retained for compatibility; existing administrator settings are durable.

    Native MCP defaults now seed only absent keys through OWUI itself. Never
    erase saved configuration just because an integration is enabled.
    """
    return None


def _local_task_headers(configs: dict, connection_env: dict[str, str]) -> dict:
    """Forward only OWUI's task TYPE (e.g. title_generation) to local bridges.

    {{TASK}} expands metadata.task, NOT task_body or message content. This
    configuration is restricted to Hubzoid-owned loopback /v1 connections.
    Remote/custom connections are not changed.
    """
    from urllib.parse import urlparse
    for index, url in enumerate(connection_env.get("OPENAI_API_BASE_URLS", "").split(";")):
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/v1":
            continue
        entry = configs.setdefault(str(index), dict(configs.get(url, {})))
        entry.setdefault("headers", {})["X-Hubzoid-Task"] = "{{TASK}}"
    return configs


def _sync_owned_connections(data_dir: Path, connection_env: dict[str, str]) -> None:
    """Refresh only Hubzoid-owned bridge wiring before OWUI reads persistence.

    Users, integrations and all other saved settings remain untouched. A failed
    update fails startup instead of serving a picker wired to stale credentials.
    """
    db = Path(data_dir) / "webui.db"
    if not db.exists():
        return
    with sqlite3.connect(db) as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='config'").fetchone():
            return
        for key, env_key in (("openai.api_base_urls", "OPENAI_API_BASE_URLS"),
                             ("openai.api_keys", "OPENAI_API_KEYS")):
            if env_key in connection_env:
                con.execute("UPDATE config SET value=? WHERE key=?",
                            (json.dumps(connection_env[env_key].split(";")), key))
        row = con.execute("SELECT value FROM config WHERE key='openai.api_configs'").fetchone()
        if row:
            configs = _local_task_headers(json.loads(row[0]), connection_env)
            con.execute("UPDATE config SET value=? WHERE key='openai.api_configs'", (json.dumps(configs),))


_DEFAULT_OWUI_ENV: dict[str, str] = {
    # Accounts are created by administrators; first-owner setup is upstream.
    # Operators may explicitly opt into email or SSO account registration.
    "ENABLE_SIGNUP": _OFF,
    "ENABLE_OAUTH_SIGNUP": _OFF,
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
    "ENABLE_API_KEY": _OFF,                  # per-user API keys defeat auth (name on OWUI <=0.9.5)
    "ENABLE_API_KEYS": _OFF,                 # same toggle, renamed in OWUI 0.9.6+
    "DEFAULT_INTERFACE_SETTINGS": '{"showChangelog": false}',
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

    # NOTE: BYPASS_MODEL_ACCESS_CONTROL is NOT set here. It is launch-mode
    # specific (single hub vs gateway), so _spawn_owui sets its default — see
    # the `bypass_model_access_control` param below.

    # --- Slim runtime: never load the local ~500MB embedding model ------
    # OWUI loads a local SentenceTransformers model (all-MiniLM-L6-v2,
    # ~500MB RAM) eagerly at startup whenever RAG_EMBEDDING_ENGINE is empty
    # — and "never using RAG" does NOT prevent it (it's loaded in main.py's
    # startup, not lazily). hubzoid strips OWUI's RAG entirely (see
    # server._normalize_owui_uploads + ENABLE_RAG_WEB_SEARCH off), so the
    # embedder is dead weight. Setting the engine to a non-empty value makes
    # OWUI's get_ef() skip the local load. This is the single deterministic
    # lever (there is no RAG_EMBEDDING_ENGINE=none). Pairs with
    # ENABLE_PERSISTENT_CONFIG off above so the env value wins every boot.
    #
    # CAVEAT (broke file attach in v0.4.x): chat never embeds, but OWUI's
    # process_file embeds every *file upload* through the configured engine
    # — with engine=openai and no key that's a 401 from api.openai.com and
    # the file is never marked processed. BYPASS_EMBEDDING_AND_RETRIEVAL
    # makes process_file extract text only (no vector store, no embedding
    # call), and at chat time the full file content flows through the same
    # <context>/<source> template that _normalize_owui_uploads strips anyway
    # (after copying the referenced file into the canonical uploads store).
    "RAG_EMBEDDING_ENGINE": "openai",        # non-empty => local MiniLM never loads
    "BYPASS_EMBEDDING_AND_RETRIEVAL": _ON,   # uploads: extract text, never embed
    "OFFLINE_MODE": _ON,                     # don't phone HuggingFace for model updates at boot
    "AUDIO_STT_ENGINE": "webapi",            # browser-side speech-to-text => 0 server RAM

    # --- Process memory allocator (glibc): cap per-thread arenas --------
    # Not an OWUI setting — it is read by the child's libc at startup, which
    # is why it belongs in the subprocess env. OWUI runs ~70 worker threads
    # (Starlette's threadpool + the imported torch/BLAS pools). glibc-malloc
    # hands each thread its own ~64MB arena and never trims it back, so RSS
    # bloats to arenas x 64MB (~4-5GB observed) of mostly-idle space — this
    # is what OOM-killed 8GB gateway boxes, NOT a model in memory (none is
    # loaded; see the slim-runtime block above). Capping arenas to 2 holds
    # OWUI to a normal footprint (~1-2GB) with no correctness change and no
    # measurable latency cost for this I/O-bound workload (it waits on the
    # LLM API and DB, not on malloc). Harmless no-op on musl/non-glibc.
    # setdefault => an operator who set MALLOC_ARENA_MAX in .env still wins.
    "MALLOC_ARENA_MAX": "2",

    # --- Real UX wins, kept on ------------------------------------------
    "ENABLE_MESSAGE_RATING": _ON,            # thumbs up/down
    "ENABLE_TITLE_GENERATION": _ON,          # auto chat titles

    # --- Admins don't read other people's chats ---------------------------
    # Off by default: an admin can't open, list or export another user's chats.
    # Both are env-only in OWUI (never settable from the admin UI), so this
    # holds every boot. Set ENABLE_ADMIN_CHAT_ACCESS=true / ENABLE_ADMIN_EXPORT=true
    # in the .env (the gateway's env in gateway mode) to allow it.
    "ENABLE_ADMIN_CHAT_ACCESS": _OFF,
    "ENABLE_ADMIN_EXPORT": _OFF,
}

# Env flipped on when a hub enables the hosted MCP server (MCP_SERVER=true).
# Users mint per-user API keys in OWUI (Settings -> Account -> API keys) and
# present them as Bearer tokens on /mcp. The endpoint-restriction pair with an
# EMPTY allowlist makes OWUI 403 every API request authenticated by key —
# verified in OWUI 0.9.6 source (utils/auth.py, get_current_user_by_api_key):
# the key mints fine but is inert against OWUI itself, so the original
# "per-user API keys defeat auth" concern stays honored. The key is an
# identity credential for the MCP surface only (hubzoid.access.owui_api_keys
# resolves it read-only against OWUI's DB). Both spellings: OWUI renamed the
# vars in 0.9.6 (plural) but still falls back to the singular forms for the
# restriction pair. `setdefault` as everywhere — the operator's .env wins.
_MCP_API_KEY_ENV: dict[str, str] = {
    "ENABLE_API_KEY": _ON,
    "ENABLE_API_KEYS": _ON,
    "USER_PERMISSIONS_FEATURES_API_KEYS": _ON,   # non-admins may mint keys; scope per-group in the admin panel instead if wanted
    "ENABLE_API_KEY_ENDPOINT_RESTRICTIONS": _ON,
    "ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS": _ON,
    "API_KEY_ALLOWED_ENDPOINTS": "",             # empty allowlist = deny-all inside OWUI
    "API_KEYS_ALLOWED_ENDPOINTS": "",
}


_OWUI_SUFFIX_NEEDLE = "    WEBUI_NAME += ' (Open WebUI)'"
_OWUI_SUFFIX_PATCH = "    pass  # hubzoid: suffix stripped (set HUBZOID_KEEP_OWUI_SUFFIX=True to restore)"


def debrand_requested(brand_dir: Path | None) -> bool:
    """Replace Open WebUI's branding only when the hub supplies its own.

    Any asset in ``<brand_dir>/branding/`` (logo, favicon, splash...) means the
    hub is branded: its files replace OWUI's and the "(Open WebUI)" name suffix
    and static titles are rebranded too. No assets: stock Open WebUI branding.
    ``HUBZOID_KEEP_OWUI_SUFFIX=true`` keeps Open WebUI's branding regardless
    (required above 50 users without an Open WebUI enterprise license).
    """
    if os.environ.get("HUBZOID_KEEP_OWUI_SUFFIX", "").strip().lower() in _TRUTHY:
        return False
    return brand_dir is not None and branding.has_assets(Path(brand_dir) / "branding")


def _patch_owui_suffix(strip: bool) -> None:
    """Patch open_webui/env.py to remove the ' (Open WebUI)' suffix.

    Open WebUI's license permits removing built-in branding for deployments
    under 50 unique end users in any rolling 30-day window, or with an
    enterprise license. So Open WebUI branding is kept unless the hub brings
    its own (see `debrand_requested`).

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


_OWUI_DEFAULT_NAME = "Open WebUI"

# Markers around the meta block hubzoid injects into OWUI's static index.html.
# Used to find/replace it idempotently on every launch (and to remove it on
# restore). OWUI's shipped index.html has a hardcoded <title>Open WebUI</title>
# and no link-share meta at all — so browser tabs and link-preview crawlers
# (which never run the JS that applies WEBUI_NAME) read "Open WebUI". This
# block fixes both surfaces statically.
_BRANDING_START = "<!-- hubzoid-branding:start -->"
_BRANDING_END = "<!-- hubzoid-branding:end -->"
_TITLE_RE = re.compile(r"<title>.*?</title>", re.IGNORECASE | re.DOTALL)
_BRANDING_BLOCK_RE = re.compile(
    re.escape(_BRANDING_START) + r".*?" + re.escape(_BRANDING_END),
    re.DOTALL,
)


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _branding_meta_block(brand: str) -> str:
    """The link-share meta tags hubzoid injects, all keyed to `brand`."""
    esc = _html_escape(brand)
    return (
        f"{_BRANDING_START}\n"
        f'\t\t<meta name="description" content="{esc}" />\n'
        f'\t\t<meta property="og:title" content="{esc}" />\n'
        f'\t\t<meta property="og:description" content="{esc}" />\n'
        f'\t\t<meta property="og:site_name" content="{esc}" />\n'
        f'\t\t<meta name="twitter:title" content="{esc}" />\n'
        f'\t\t<meta name="twitter:description" content="{esc}" />\n'
        f"\t\t{_BRANDING_END}"
    )


def _patch_index_html(path: Path, brand: str, *, strip: bool) -> None:
    """Rewrite <title> and the hubzoid meta block in one OWUI index.html.

    `strip=True` rebrands to `brand`; `strip=False` restores OWUI's default
    title and removes the injected meta block. Idempotent either way.
    """
    try:
        text = path.read_text()
    except OSError:
        return
    original = text

    if strip:
        title = f"<title>{_html_escape(brand)}</title>"
        if _TITLE_RE.search(text):
            text = _TITLE_RE.sub(lambda _m: title, text, count=1)
        block = _branding_meta_block(brand)
        if _BRANDING_START in text:
            text = _BRANDING_BLOCK_RE.sub(lambda _m: block, text, count=1)
        else:
            # Drop the block right after the (now rebranded) <title>.
            text = text.replace(title, f"{title}\n\t\t{block}", 1)
    else:
        text = _TITLE_RE.sub(
            lambda _m: f"<title>{_OWUI_DEFAULT_NAME}</title>", text, count=1
        )
        # Remove our block plus the trailing whitespace/newline we added.
        text = re.sub(r"\n\t\t" + _BRANDING_BLOCK_RE.pattern, "", text)
        text = _BRANDING_BLOCK_RE.sub("", text)

    if text != original:
        try:
            path.write_text(text)
        except OSError:
            return


def _patch_webmanifest(path: Path, brand: str, *, strip: bool) -> None:
    """Rewrite name/short_name in one OWUI site.webmanifest."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    target = brand if strip else _OWUI_DEFAULT_NAME
    short = brand if strip else "WebUI"
    if data.get("name") == target and data.get("short_name") == short:
        return
    data["name"] = target
    data["short_name"] = short
    try:
        path.write_text(json.dumps(data, indent=2))
    except OSError:
        return


def _patch_owui_branding(brand: str, *, strip: bool) -> None:
    """Replace OWUI's static "Open WebUI" branding with `brand`.

    Covers the surfaces WEBUI_NAME does NOT reach because they are served
    as static files (no runtime substitution): the index.html <title> a
    browser tab shows before the SPA hydrates, the link-preview meta a
    crawler reads, and the PWA site.webmanifest name. Gated on the same
    decision as the "(Open WebUI)" suffix patch (`debrand_requested`); when
    Open WebUI branding is kept, this restores the defaults instead. Idempotent; reverted by a pip upgrade
    and re-applied on the next `hubzoid run`.
    """
    for static_dir in branding.static_dirs():
        index = static_dir / "index.html"
        if index.is_file():
            _patch_index_html(index, brand, strip=strip)
        for manifest in (
            static_dir / "site.webmanifest",
            static_dir / "static" / "site.webmanifest",
        ):
            if manifest.is_file():
                _patch_webmanifest(manifest, brand, strip=strip)


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
    ui_host: str = "127.0.0.1",
    api_key: str,
    model_label: str,
    webui_name: str | None,
    suggestions: list[str] | None = None,
    response_watermark: str | None = None,
    enable_api_keys: bool = False,
) -> subprocess.Popen:
    """Spawn Open WebUI as a subprocess. Returns the Popen handle.

    `suggestions` populates the new-chat quick-start buttons. Sourced from
    the main agent's AGENTS.md frontmatter (`suggestions:` field).
    `response_watermark` defaults to the hub folder name when None.
    """
    # Single-hub wiring: one bridge, one model. These OPENAI_* values are
    # not operator-overridable — they are how hubzoid joins the bridge to
    # OWUI (applied via direct assignment in _spawn_owui).
    connection_env = {
        "OPENAI_API_BASE_URL": f"http://127.0.0.1:{bridge_port}/v1",
        "OPENAI_API_BASE_URLS": f"http://127.0.0.1:{bridge_port}/v1",
        "OPENAI_API_KEY": api_key,
        "OPENAI_API_KEYS": api_key,
        "DEFAULT_MODELS": model_label,
        # Forward the logged-in user's identity to the bridge so per-role tool
        # access can resolve their groups. OWUI sends X-OpenWebUI-User-Email
        # (+ id/name/role); the bridge maps the email to the user's OWUI groups.
        # See hubzoid.access.owui_groups.
        "ENABLE_FORWARD_USER_INFO_HEADERS": "true",
    }
    return _spawn_owui(
        data_dir=hub_dir / ".openwebui-data",
        ui_host=ui_host,
        ui_port=ui_port,
        connection_env=connection_env,
        webui_name=webui_name,
        response_watermark=response_watermark or hub_dir.name,
        suggestions=suggestions,
        # One model per hub: OWUI 0.9.6 hides base/connection models (no
        # Workspace DB row) from non-admin users, so without this every invited
        # user gets an empty model selector → "Model not selected". Nothing to
        # scope at the model level in a single hub, so bypass by default.
        bypass_model_access_control=True,
        enable_api_keys=enable_api_keys,
        debrand=debrand_requested(hub_dir),
    )


def start_gateway(
    *,
    data_dir: Path,
    ui_port: int,
    connection_env: dict[str, str],
    ui_host: str = "127.0.0.1",
    webui_name: str | None = None,
    response_watermark: str | None = None,
    suggestions: list[str] | None = None,
    enable_api_keys: bool = False,
    brand_dir: Path | None = None,
) -> subprocess.Popen:
    """Spawn ONE Open WebUI fronting many bridges (the `hubzoid gateway` path).

    `connection_env` carries OWUI's multi-connection wiring
    (`OPENAI_API_BASE_URLS` / `OPENAI_API_KEYS`, semicolon-separated) built by
    `hubzoid.gateway.GatewayPlan.connection_env`. Same strip/branding/auth
    hardening as `start`; only the connection shape differs.
    """
    return _spawn_owui(
        data_dir=Path(data_dir),
        ui_host=ui_host,
        ui_port=ui_port,
        connection_env=connection_env,
        webui_name=webui_name,
        response_watermark=response_watermark or "hubzoid",
        suggestions=suggestions,
        # Gateway fronts MANY models and its whole purpose is per-team isolation
        # (per-model Private ACLs + group assignment — see docs/DEPLOYING.md).
        # Bypassing model access control would let every user see every team's
        # model, so it is FORCED off here (even over an inherited env value —
        # see _spawn_owui 5b). Operators opt out of isolation with
        # HUBZOID_GATEWAY_ALLOW_BYPASS=1.
        bypass_model_access_control=False,
        enable_api_keys=enable_api_keys,
        debrand=debrand_requested(brand_dir),
    )


def _spawn_owui(
    *,
    data_dir: Path,
    ui_host: str,
    ui_port: int,
    connection_env: dict[str, str],
    webui_name: str | None,
    response_watermark: str,
    suggestions: list[str] | None,
    bypass_model_access_control: bool = False,
    enable_api_keys: bool = False,
    debrand: bool = False,
) -> subprocess.Popen:
    """Shared Open WebUI launcher for both `start` and `start_gateway`.

    `connection_env` is applied with direct assignment (hubzoid owns the
    bridge wiring); everything else is `setdefault` so the operator's `.env`
    wins.
    """
    # Open WebUI branding stays unless the hub brings its own (debrand, see
    # debrand_requested): then the "(Open WebUI)" suffix goes, and so do the
    # static surfaces WEBUI_NAME can't reach (tab title before hydration,
    # link-preview meta, PWA manifest). Otherwise both are restored.
    _patch_owui_suffix(strip=debrand)
    _patch_owui_branding(webui_name or "Hubzoid", strip=debrand)

    binary = _find_binary()
    if binary is None:
        raise FileNotFoundError(
            "open-webui not found next to the running Python or on PATH. "
            "It is bundled with hubzoid; reinstall to repair:\n"
            "    pip install --force-reinstall hubzoid\n"
            "or install it directly:\n"
            "    pip install open-webui"
        )

    data_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()

    # 1. Wiring + per-hub state. Not operator-overridable.
    env["DATA_DIR"] = str(data_dir)
    configs = _local_task_headers(json.loads(env.get("OPENAI_API_CONFIGS", "{}")), connection_env)
    connection_env = {**connection_env, "OPENAI_API_CONFIGS": json.dumps(configs)}
    env.update(connection_env)
    _sync_owned_connections(data_dir, connection_env)

    # 2. Auth default off for local dev. Operator overrides via .env.
    env.setdefault("WEBUI_AUTH", "False")

    # 3. Branding. WEBUI_NAME cascades via the cli's resolver; we just pass
    # the resolved value. RESPONSE_WATERMARK defaults silently.
    if webui_name:
        env.setdefault("WEBUI_NAME", webui_name)
    env.setdefault("RESPONSE_WATERMARK", response_watermark)

    # 4. Quick-start prompt suggestions. OWUI expects a JSON array of objects.
    if suggestions:
        payload = [{"content": s} for s in suggestions if s]
        env.setdefault("DEFAULT_PROMPT_SUGGESTIONS", json.dumps(payload))

    # 5. MCP credential minting. Must precede the defaults loop: both write
    # the ENABLE_API_KEY* names via setdefault, and the _ON set only wins by
    # getting there first. Operator .env still beats both.
    if enable_api_keys:
        for key, value in _MCP_API_KEY_ENV.items():
            env.setdefault(key, value)

    # 5c. OWUI-native per-user MCP: one switch. Applied BEFORE the strip so its
    # ENABLE_PERSISTENT_CONFIG=True wins over the strip's default of False.
    # setdefault throughout, so an operator's explicit .env value still wins.
    if os.environ.get("OWUI_NATIVE_MCP", "").strip().lower() in _TRUTHY:
        for key, value in _OWUI_NATIVE_MCP_ENV.items():
            env.setdefault(key, value)
        # Preserve saved integration settings while enabling native MCP defaults.
        _seed_owui_config_once(data_dir)

    # 6. The big strip. Apply hubzoid defaults; operator .env wins.
    for key, value in _DEFAULT_OWUI_ENV.items():
        env.setdefault(key, value)

    # 5b. Model-list visibility for non-admin (role=user) accounts. OWUI 0.9.6
    # filters base/connection models (those without a Workspace DB row) out of
    # the list for users, leaving them with "Model not selected". Single hubs
    # have exactly one model and nothing to scope, so they bypass the filter
    # (setdefault => the operator's .env value wins). The gateway is different:
    # per-team isolation IS its point, and BYPASS...=True is the documented
    # single-hub fix, so it leaks into gateway environments easily — where it
    # would silently show every team every other team's agent. The gateway
    # therefore FORCES the filter on, warning if it had to override; an
    # operator who really wants the bypass sets HUBZOID_GATEWAY_ALLOW_BYPASS=1.
    if bypass_model_access_control:
        env.setdefault("BYPASS_MODEL_ACCESS_CONTROL", _ON)
    else:
        allow = os.environ.get("HUBZOID_GATEWAY_ALLOW_BYPASS", "").strip().lower() in _TRUTHY
        if allow:
            # Operator explicitly wants an open gateway: the flag alone
            # suffices (defaults the bypass ON); an explicit
            # BYPASS_MODEL_ACCESS_CONTROL value still wins over the default.
            env.setdefault("BYPASS_MODEL_ACCESS_CONTROL", _ON)
        else:
            if env.get("BYPASS_MODEL_ACCESS_CONTROL", "").strip().lower() in _TRUTHY:
                log.warning(
                    "BYPASS_MODEL_ACCESS_CONTROL=True found in the environment — "
                    "that is the single-hub fix and would disable per-team model "
                    "isolation in gateway mode; forcing it off. Set "
                    "HUBZOID_GATEWAY_ALLOW_BYPASS=1 for an open gateway."
                )
            env["BYPASS_MODEL_ACCESS_CONTROL"] = _OFF

    # 7. Refuse to boot if auth is on with an unsafe config.
    _validate_auth_env(env)

    log_path = data_dir / "openwebui.log"
    log_file = log_path.open("ab", buffering=0)
    cmd = [binary, "serve", "--host", ui_host, "--port", str(ui_port)]
    proc = subprocess.Popen(cmd, env=env, stdout=log_file, stderr=subprocess.STDOUT)
    proc._log_path = log_path  # type: ignore[attr-defined]
    return proc
