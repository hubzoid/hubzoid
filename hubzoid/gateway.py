"""Gateway planning — one Open WebUI fronting many hub bridges.

`hubzoid run` is one bridge + one Open WebUI per hub. That is full isolation
but N heavy OWUI processes. For a team-of-teams deployment (IRS hub, GPMS
hub, …) where the weight matters and per-team *access* — not per-team URLs —
is what's wanted, `hubzoid gateway` runs **one** Open WebUI over **N**
bridges:

  * Each hub runs as a headless bridge (`hubzoid run <hub> --no-ui`), each on
    its own `BRIDGE_PORT`.
  * One Open WebUI connects to all of them via OWUI's multi-connection env
    (`OPENAI_API_BASE_URLS` / `OPENAI_API_KEYS`, semicolon-separated). Each
    bridge surfaces as a selectable model; OWUI's Groups + per-model Private
    ACL gate which team sees which agent. One login surface, shared branding.
  * The edge router gives each hub its own artifact prefix
    (`/b/<slug>/artifacts/*`) so download links route to the owning bridge.

This module is pure planning — no processes are launched here. The CLI
`gateway` command executes the plan. Keeping it pure makes the wiring
(connection env, edge routes, key/url alignment) unit-testable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import settings as settingslib


@dataclass(frozen=True)
class GatewayBackend:
    hub_dir: Path
    slug: str          # unique, URL-safe; namespaces this hub's artifacts
    bridge_port: int
    api_key: str       # the bridge's first BRIDGE_API_KEYS entry
    model_label: str   # what /v1/models reports (best-effort, for display)
    mcp: bool = False  # hub serves /mcp (MCP_SERVER=true in its .env)
    mcp_access_group: str = ""  # OWUI group gating this hub's /mcp ("" = any user)
    # WhatsApp/Telegram inbound: True when this hub's .env configures a webhook
    # surface. The gateway edge then forwards /webhooks/* to its loopback inbound
    # server; without it the webhook falls through to Open WebUI's catch-all.
    inbound: bool = False
    inbound_port: int = 0  # the hub's loopback inbound server port (0 = unset)
    # Per-hub display identity, read from AGENTS.md frontmatter + branding/.
    # Consumed by gateway_provision to seed each hub's Open WebUI model entry
    # (picker name, description, quick-start suggestions, avatar). All optional;
    # empty defaults mean "provision skips that field", never an error.
    display_name: str = ""       # AGENTS.md `name:`, falls back to folder name
    description: str = ""        # AGENTS.md `description:`
    suggestions: tuple[str, ...] = ()  # AGENTS.md `suggestions:` list
    logo: Path | None = None     # <hub>/branding/ logo file, if any


@dataclass(frozen=True)
class GatewayPlan:
    backends: tuple[GatewayBackend, ...]

    @property
    def base_urls(self) -> list[str]:
        return [f"http://127.0.0.1:{b.bridge_port}/v1" for b in self.backends]

    @property
    def api_keys(self) -> list[str]:
        return [b.api_key for b in self.backends]

    def connection_env(self) -> dict[str, str]:
        """OWUI multi-connection env. Semicolons, positionally aligned."""
        env = {
            "ENABLE_OPENAI_API": "True",
            "OPENAI_API_BASE_URLS": ";".join(self.base_urls),
            "OPENAI_API_KEYS": ";".join(self.api_keys),
            # Forward the logged-in user's identity to every bridge, as the
            # single-hub path does (webui.start). Without it OWUI never sends
            # X-OpenWebUI-User-Email, so the bridge derives an anonymous identity
            # and every restricted tool is denied ("anonymous") — access control
            # is dead in gateway mode. Defaults on, but an operator's explicit
            # environment value wins (e.g. --no-bridges fronting external
            # bridges that must not receive user PII).
            "ENABLE_FORWARD_USER_INFO_HEADERS":
                os.environ.get("ENABLE_FORWARD_USER_INFO_HEADERS", "true"),
        }
        labels = [b.model_label for b in self.backends if b.model_label]
        if labels:
            env["DEFAULT_MODELS"] = labels[0]
        return env

    def edge_routes(self, *, artifact_prefix: str = "/artifacts") -> list[dict]:
        """Per-hub routes for the edge: /b/<slug>/artifacts -> bridge, plus
        /b/<slug>/mcp for MCP-enabled hubs.

        `strip_prefix` removes `/b/<slug>` so the bridge sees its native
        `/artifacts/...` (or `/mcp`) path. Only MCP-enabled hubs get an /mcp
        route — the bridge wouldn't serve it anyway, but the edge should not
        even forward the path.
        """
        routes = []
        for b in self.backends:
            base = f"/b/{b.slug}"
            routes.append({
                "prefix": base + artifact_prefix,
                "upstream": f"http://127.0.0.1:{b.bridge_port}",
                "strip_prefix": base,
            })
            if b.mcp:
                routes.append({
                    "prefix": base + "/mcp",
                    "upstream": f"http://127.0.0.1:{b.bridge_port}",
                    "strip_prefix": base,
                })
        # WhatsApp/Telegram inbound: the hub's inbound server owns /webhooks/*
        # (e.g. /webhooks/whatsapp) on a loopback port, each POST signature- or
        # secret-verified before anything runs. The edge must forward it or the
        # provider's webhook falls through to Open WebUI. /webhooks is a single
        # global prefix (the inbound app is not namespaced per hub), so one
        # fronted hub can own it: the first with an inbound surface wins. No
        # strip_prefix — the inbound app serves the full /webhooks/... path.
        inbound = next((b for b in self.backends if b.inbound), None)
        if inbound:
            routes.append({
                "prefix": "/webhooks",
                "upstream": f"http://127.0.0.1:{inbound.inbound_port}",
            })
        return routes

    @property
    def any_mcp(self) -> bool:
        """True when at least one fronted hub serves /mcp — the gateway then
        enables OWUI per-user API-key minting (locked to deny-all inside
        OWUI; see webui._MCP_API_KEY_ENV)."""
        return any(b.mcp for b in self.backends)

    def branding_source(self, gw_data: Path, override: str | None = None) -> Path:
        """The directory whose ``branding/`` subdir provides the gateway's
        shared OWUI chrome (favicon, splash, sidebar logo).

        One shared OWUI fronts N hubs, so the chrome is org-level. Resolution,
        in priority order:

        1. ``override`` (from ``HUBZOID_GATEWAY_BRANDING``) — a hub *slug* to
           borrow that hub's branding, or a filesystem *path* to a dir that
           contains a ``branding/`` subdir.
        2. ``<gw_data>/branding`` when populated — an explicit gateway-owned
           branding dir (backward compatible with the state-dir convention).
        3. the **first hub** in the list — the zero-config default, so a hub's
           own ``branding/`` brands the shared chrome with no extra steps.
        4. ``gw_data`` — nothing to stamp; only the baseline CSS is written.

        Returns a dir suitable for ``branding.apply(<dir>, ...)`` (which reads
        ``<dir>/branding``). Never raises.
        """
        from . import branding as _branding

        if override and override.strip():
            override = override.strip()
            for b in self.backends:
                if b.slug == override:
                    return b.hub_dir
            p = Path(override).expanduser()
            if p.is_dir():
                return p
        if _branding.has_assets(gw_data / "branding"):
            return gw_data
        if self.backends:
            return self.backends[0].hub_dir
        return gw_data

    def public_url_for(self, public_base: str, backend: GatewayBackend) -> str:
        """The HUBZOID_PUBLIC_URL a given bridge should advertise, so its
        artifact links resolve through the edge to itself."""
        return public_base.rstrip("/") + f"/b/{backend.slug}"


def _slugify(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in str(text).strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "hub"


def plan(hub_dirs: list[Path], *, load=settingslib.load) -> GatewayPlan:
    """Build a GatewayPlan from hub directories.

    Reads each hub's `.env` for its bridge port + first bridge key + label.
    Slugs are de-duplicated so two hubs never collide on an artifact prefix.
    Raises ValueError if two hubs share a bridge port (they'd race).
    """
    backends: list[GatewayBackend] = []
    seen_slugs: dict[str, int] = {}
    seen_ports: dict[int, Path] = {}
    seen_labels: dict[str, Path] = {}
    for hub_dir in hub_dirs:
        hub_dir = Path(hub_dir).resolve()
        s = load(hub_dir)
        if s.bridge_port in seen_ports:
            raise ValueError(
                f"gateway: hubs {seen_ports[s.bridge_port].name} and "
                f"{hub_dir.name} both use BRIDGE_PORT={s.bridge_port}; give "
                f"each hub a unique BRIDGE_PORT in its .env."
            )
        seen_ports[s.bridge_port] = hub_dir

        base_slug = _slugify(hub_dir.name)
        n = seen_slugs.get(base_slug, 0)
        seen_slugs[base_slug] = n + 1
        slug = base_slug if n == 0 else f"{base_slug}-{n + 1}"

        meta = _agent_meta(hub_dir)
        label = s.model_label or _bridge_model_label(meta["fm_name"], hub_dir)
        if label in seen_labels:
            raise ValueError(
                f"gateway: hubs {seen_labels[label].name} and {hub_dir.name} "
                f"both surface as model '{label}'. Each hub must expose a "
                "unique model id, or one team's chats would silently route to "
                "the other team's agent. Give one of them a distinct `name:` "
                "in AGENTS.md or a MODEL_LABEL in its .env."
            )
        seen_labels[label] = hub_dir
        backends.append(GatewayBackend(
            hub_dir=hub_dir,
            slug=slug,
            bridge_port=s.bridge_port,
            api_key=s.first_api_key,
            model_label=label,
            mcp=_mcp_enabled(hub_dir),
            mcp_access_group=_own_env_value(hub_dir, "MCP_ACCESS_GROUP"),
            inbound=_inbound_enabled(hub_dir),
            inbound_port=_inbound_port_for(hub_dir),
            display_name=meta["fm_name"] or hub_dir.name,
            description=meta["description"],
            suggestions=meta["suggestions"],
            logo=_find_logo(hub_dir),
        ))
    return GatewayPlan(backends=tuple(backends))


def _own_env_value(hub_dir: Path, key: str) -> str:
    """A value from this hub's own `.env` file — and only from there.

    Read with `dotenv_values` (no process-env mutation) instead of
    `settings.load`: the plan loop loads N hubs' .env files into os.environ
    sequentially with override=True, so a value left behind by hub A would
    bleed into hub B's settings if we read the environment here. MCP flags
    must never leak across hubs, so they are per-file only in gateway mode.
    (A bridge process reads them normally via settings.load — one process,
    one hub, no bleed.)
    """
    from dotenv import dotenv_values

    env_path = Path(hub_dir) / ".env"
    if not env_path.is_file():
        return ""
    return ((dotenv_values(env_path) or {}).get(key) or "").strip()


def _mcp_enabled(hub_dir: Path) -> bool:
    """Whether this hub's own `.env` sets MCP_SERVER=true."""
    from . import settings as settingslib

    return settingslib.truthy(_own_env_value(hub_dir, "MCP_SERVER"))


def _own_env(hub_dir: Path) -> dict:
    """This hub's own `.env` as a dict, read without mutating process env.

    Same isolation rationale as `_own_env_value`: the plan loop loads N hubs'
    .env files with override=True, so surface detection must read the file
    directly and never the environment, or one hub's WHATSAPP_* would bleed
    into the next hub's plan.
    """
    from dotenv import dotenv_values

    env_path = Path(hub_dir) / ".env"
    if not env_path.is_file():
        return {}
    return {k: (v or "") for k, v in (dotenv_values(env_path) or {}).items()}


def _inbound_enabled(hub_dir: Path) -> bool:
    """Whether this hub's own `.env` configures a WhatsApp or Telegram surface,
    so the gateway edge should forward /webhooks/* to its inbound server.

    Uses the same token checks the inbound server itself uses, so the edge route
    and the served routes can never disagree about whether a surface is on.
    """
    from .inbound.env import missing_telegram_vars, missing_whatsapp_vars

    own = _own_env(hub_dir)
    return not missing_whatsapp_vars(own) or not missing_telegram_vars(own)


def _inbound_port_for(hub_dir: Path) -> int:
    """This hub's inbound-server port: HUBZOID_INBOUND_PORT from its own .env,
    else the default. Resolved with the inbound module's own helper so gateway
    and inbound agree on the port."""
    from .inbound.run import inbound_port

    return inbound_port({"HUBZOID_INBOUND_PORT": _own_env_value(hub_dir, "HUBZOID_INBOUND_PORT")})


def _bridge_model_label(fm_name: str | None, hub_dir: Path) -> str:
    """The model id THE BRIDGE will report at /v1/models, derived with the
    bridge's own functions (loaders.agents name fallback + server slugify) so
    the provisioned OWUI entry and the live connection model can never drift
    apart on fallback names."""
    from .loaders.agents import _safe_id
    from .server import _slugify as server_slugify
    return server_slugify(fm_name or _safe_id(hub_dir.name))


def _agent_meta(hub_dir: Path) -> dict:
    """Best-effort hub identity from AGENTS.md frontmatter. Never raises:
    a missing/broken AGENTS.md yields fm_name=None + empty fields, matching
    the fail-safe posture of everything provisioning-related. fm_name stays
    None when frontmatter has no `name:` so callers can apply the SAME
    fallback the bridge applies (see _bridge_model_label)."""
    from . import frontmatter as fm
    fm_name, description, suggestions = None, "", ()
    try:
        data, _ = fm.read(hub_dir / "AGENTS.md")
        raw_name = data.get("name")
        fm_name = str(raw_name) if raw_name else None
        description = str(data.get("description") or "")
        raw = data.get("suggestions") or []
        if isinstance(raw, list):
            suggestions = tuple(str(s) for s in raw if str(s).strip())
    except Exception:  # noqa: BLE001
        pass
    return {"fm_name": fm_name, "description": description, "suggestions": suggestions}


def _find_logo(hub_dir: Path) -> Path | None:
    """The hub's raster logo for use as its Open WebUI model avatar.

    Only raster formats OWUI's profile-image validator accepts (it rejects
    SVG data-URIs as script-capable). Prefers logo.* over favicon.*.
    """
    branding_dir = hub_dir / "branding"
    if not branding_dir.is_dir():
        return None
    files = {e.name.lower(): e for e in branding_dir.iterdir() if e.is_file()}
    for stem in ("logo", "favicon"):
        for ext in ("png", "webp", "jpg", "jpeg", "gif"):
            match = files.get(f"{stem}.{ext}")
            if match is not None:
                return match
    return None
