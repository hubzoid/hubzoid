"""Gateway planning — many hub bridges behind one front door.

`hubzoid gateway` runs N bridges and one edge. Every bridge serves the Hubzoid
web app and shares the operational store (sessions, accounts, conversations,
grants), so any bridge can answer a deployment-wide call. The edge sends
`/b/<slug>/api/*`, `/b/<slug>/artifacts/*`, `/b/<slug>/mcp` and
`/b/<slug>/branding/*` to that hub's bridge with the prefix stripped
(`edge_routes`), `/webhooks/<slug>/*` to that hub's inbound server, and
everything else to the first bridge. The chat app calls hub-scoped routes at
`${api_base}/api/...` with `api_base` = `/b/<slug>`.

This module is pure planning — no processes are launched here. The CLI
`gateway` command executes the plan. Keeping it pure makes the wiring (edge
routes, key/url alignment) unit-testable.
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
    mcp_public_url: str = ""
    mcp: bool = False  # hub serves /mcp (MCP_SERVER=true in its .env)
    # WhatsApp/Telegram inbound: True when this hub's .env configures a webhook
    # surface. The gateway edge then forwards /webhooks/* to its loopback inbound
    # server.
    inbound: bool = False
    inbound_port: int = 0  # the hub's loopback inbound server port (0 = unset)
    # Per-hub display name (AGENTS.md `name:`, falls back to the folder name),
    # recorded in the deployment manifest.
    display_name: str = ""


@dataclass(frozen=True)
class GatewayPlan:
    backends: tuple[GatewayBackend, ...]

    def edge_routes(self, *, artifact_prefix: str = "/artifacts") -> list[dict]:
        """Per-hub routes for the edge: /b/<slug>/artifacts, /b/<slug>/api and
        /b/<slug>/branding (the hub-scoped web app calls) -> bridge, plus
        /api/hubs/<key> (the hub-scoped API, by hub key, unstripped), plus
        /b/<slug>/mcp for MCP-enabled hubs, plus /webhooks/<slug> for inbound hubs.

        `strip_prefix` removes `/b/<slug>` so the bridge sees its native
        `/artifacts/...` (or `/mcp`, `/api/...`, `/branding/...`) path. Only
        MCP-enabled hubs get an /mcp route — the bridge wouldn't serve it anyway,
        but the edge should not even forward the path. The /webhooks/<slug>
        route (no strip) reaches the hub's own inbound server; every inbound hub
        gets one, so several can run behind one front door.
        """
        routes = []
        for b in self.backends:
            first = len(routes)
            base = f"/b/{b.slug}"
            routes.append({
                "prefix": base + artifact_prefix,
                "upstream": f"http://127.0.0.1:{b.bridge_port}",
                "strip_prefix": base,
            })
            routes.append({"prefix": base + "/portal/api",
                           "upstream": f"http://127.0.0.1:{b.bridge_port}",
                           "strip_prefix": base})
            # Hub-scoped API by hub key, not slug: /api/hubs/<key>/* reaches this
            # hub's bridge unchanged, and the bridge refuses any other key.
            from .assertions import hub_key

            routes.append({"prefix": f"/api/hubs/{hub_key(b.hub_dir)}",
                           "upstream": f"http://127.0.0.1:{b.bridge_port}"})
            # The OpenAI-compatible chat API of this agent, for personal API
            # keys (the edge refuses every other credential there).
            for part in ("/api", "/branding", "/v1"):
                routes.append({
                    "prefix": base + part,
                    "upstream": f"http://127.0.0.1:{b.bridge_port}",
                    "strip_prefix": base,
                })
            if b.mcp:
                routes.append({
                    "prefix": base + "/mcp",
                    "upstream": f"http://127.0.0.1:{b.bridge_port}",
                    "strip_prefix": base,
                })
                for prefix in (f"/.well-known/oauth-protected-resource{base}/mcp",
                               f"/.well-known/oauth-authorization-server{base}/mcp/oauth"):
                    routes.append({"prefix": prefix, "upstream": f"http://127.0.0.1:{b.bridge_port}"})
            # Inbound surfaces (WhatsApp/Telegram/generic webhook): the hub's
            # inbound server owns /webhooks/<slug>/* (e.g. /webhooks/<slug>/whatsapp)
            # on a loopback port, each POST signature-, secret-, or HMAC-verified
            # before anything runs. The edge must forward it. The route is namespaced by hub
            # slug so EVERY inbound hub is reachable at once (before 0.9.1 a single
            # global /webhooks meant only the first inbound hub could be reached).
            # No strip_prefix — the inbound app serves the full /webhooks/<slug>/...
            # path, matching the slug it derives from its own hub folder.
            from .workflows.events import route_declarations
            for name in route_declarations(b.hub_dir):
                routes.append({"prefix": f"/webhooks/{b.slug}/{name}",
                               "upstream": f"http://127.0.0.1:{b.bridge_port}"})
            if b.inbound:
                routes.append({
                    "prefix": f"/webhooks/{b.slug}",
                    "upstream": f"http://127.0.0.1:{b.inbound_port}",
                })
            for r in routes[first:]:
                r["hub"] = b.slug  # the edge answers for it by this hub's state
        return routes

    @property
    def any_mcp(self) -> bool:
        """True when at least one fronted hub serves OAuth-protected MCP."""
        return any(b.mcp for b in self.backends)

    def branding_source(self, gw_data: Path, override: str | None = None) -> Path:
        """The directory whose ``branding/`` subdir provides the gateway's
        shared page chrome (favicon, splash, logo).

        One web app fronts N hubs, so the chrome is org-level. Resolution,
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
    from . import datadir

    backends: list[GatewayBackend] = []
    seen_slugs: dict[str, int] = {}
    seen_ports: dict[int, Path] = {}
    seen_inbound_ports: dict[int, Path] = {}
    seen_labels: dict[str, Path] = {}
    # Ports hubs pin in their own .env; the others take the next free one.
    pinned_ports = {int(p) for p in (_own_env_value(Path(h), "BRIDGE_PORT") for h in hub_dirs) if p.isdigit()}
    for hub_dir in hub_dirs:
        hub_dir = Path(hub_dir).resolve()
        # Loading a hub's dotenv is local to that hub. Do not carry its secrets
        # or ports into the next hub or the shared OWUI/edge processes.
        inherited_env = os.environ.copy()
        try:
            s = load(hub_dir)
            data = datadir.configured()  # as this hub sees it, its .env included
        finally:
            os.environ.clear()
            os.environ.update(inherited_env)
        if data is not None:
            # Other processes of a gateway (the edge, backups, the other bridges)
            # find a hub's state in its folder. HUBZOID_DATA_DIR is for one
            # hosted hub (`hubzoid run`), not a gateway.
            raise ValueError(f"gateway: hub {hub_dir.name} sets {datadir.ENV}={data}. A gateway keeps each "
                             f"hub's state in that hub's folder and its own in --data-dir. Unset "
                             f"{datadir.ENV}, or run this hub on its own with `hubzoid run`.")
        bridge_port = s.bridge_port
        pinned = bool(_own_env_value(hub_dir, "BRIDGE_PORT") or inherited_env.get("BRIDGE_PORT"))
        if not pinned:
            # No port of its own (`hubzoid init` leaves it unset): the next one
            # no other hub uses or pins.
            while bridge_port in seen_ports or bridge_port in pinned_ports:
                bridge_port += 1
        if bridge_port in seen_ports:
            raise ValueError(
                f"gateway: hubs {seen_ports[bridge_port].name} and "
                f"{hub_dir.name} both use BRIDGE_PORT={bridge_port}; give "
                f"each hub a unique BRIDGE_PORT in its .env, or remove the line."
            )
        seen_ports[bridge_port] = hub_dir

        base_slug = _slugify(hub_dir.name)
        n = seen_slugs.get(base_slug, 0)
        seen_slugs[base_slug] = n + 1
        slug = base_slug if n == 0 else f"{base_slug}-{n + 1}"

        mcp_public_url = _own_env_value(hub_dir, "MCP_PUBLIC_URL")
        if _mcp_enabled(hub_dir):
            from urllib.parse import urlsplit
            from .mcp_oauth import validate_public_url
            mcp_public_url = validate_public_url(mcp_public_url)
            if urlsplit(mcp_public_url).path != f"/b/{slug}/mcp":
                raise ValueError(f"MCP_PUBLIC_URL for {hub_dir.name} must end in /b/{slug}/mcp")

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

        # Two inbound hubs sharing an inbound port would collide on the loopback
        # bind, so the edge could reach only one. Caught here with the same
        # posture as the bridge-port guard: each inbound hub needs a distinct
        # HUBZOID_INBOUND_PORT in its own .env.
        inbound_on = _inbound_enabled(hub_dir)
        inbound_port = _inbound_port_for(hub_dir)
        if inbound_on and not _own_env_value(hub_dir, "HUBZOID_INBOUND_PORT"):
            # No port chosen in the hub's .env: the next free one from the
            # default, so several inbound hubs never collide.
            while inbound_port in seen_inbound_ports:
                inbound_port += 1
        if inbound_on:
            if inbound_port in seen_inbound_ports:
                raise ValueError(
                    f"gateway: hubs {seen_inbound_ports[inbound_port].name} and "
                    f"{hub_dir.name} both use HUBZOID_INBOUND_PORT={inbound_port} "
                    "for their inbound webhook server; give each inbound hub a "
                    "unique HUBZOID_INBOUND_PORT in its .env."
                )
            seen_inbound_ports[inbound_port] = hub_dir

        backends.append(GatewayBackend(
            hub_dir=hub_dir,
            slug=slug,
            bridge_port=bridge_port,
            api_key=s.first_api_key,
            model_label=label,
            mcp_public_url=mcp_public_url,
            mcp=_mcp_enabled(hub_dir),
            inbound=inbound_on,
            inbound_port=inbound_port,
            display_name=meta["fm_name"] or hub_dir.name,
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


# Sign-in settings that belong to the whole deployment. Up to 0.9.x the gateway
# picked them up from the hub .env files it read (the last hub listed won), and
# existing deployments still keep them there. Open WebUI's own names are not
# inherited: a hub that still sets one stops the gateway (`appmode.RETIRED`).
_DEPLOYMENT_PREFIXES = ("OAUTH_", "OPENID_", "GOOGLE_CLIENT_", "MICROSOFT_CLIENT_", "GITHUB_CLIENT_")
_DEPLOYMENT_KEYS = frozenset({"ENABLE_SIGNUP", "ENABLE_LOGIN_FORM", "ENABLE_PASSWORD_AUTH",
                              "ENABLE_OAUTH_SIGNUP", "HUBZOID_PUBLIC_URL"})


def _deployment_key(key: str) -> bool:
    return key in _DEPLOYMENT_KEYS or key.startswith(_DEPLOYMENT_PREFIXES)


def inherited_deployment_env(hub_dirs: list[Path], env) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Deployment-wide settings the gateway's own environment lacks, taken from
    the hubs' own `.env` files.

    Returns `(values, conflicts)`. A key set by several hubs takes the last
    listed hub's value, as before. `conflicts` names the hubs whose values differ,
    by key, so the caller can warn without printing a value. Nothing else in a
    hub's `.env` (model keys, restricted-tool secrets) reaches the gateway.
    """
    values: dict[str, str] = {}
    seen: dict[str, dict[str, str]] = {}
    for hub_dir in hub_dirs:
        for key, value in _own_env(Path(hub_dir)).items():
            if not value or not _deployment_key(key) or env.get(key):
                continue
            values[key] = value
            seen.setdefault(key, {})[Path(hub_dir).name] = value
    conflicts = {k: sorted(v) for k, v in seen.items() if len(set(v.values())) > 1}
    return values, conflicts


def _inbound_enabled(hub_dir: Path) -> bool:
    """Whether this hub runs the 1.x inbound server (WhatsApp, Telegram or a
    generic webhook), so the edge forwards /webhooks/<slug>/* to it: its
    channels/ files say so (hubzoid.channels.legacy), from its own `.env`
    values, never the environment another hub's .env left behind."""
    from .channels import legacy

    return bool(legacy.plan(hub_dir, _own_env(hub_dir)).inbound)


def _inbound_port_for(hub_dir: Path) -> int:
    """This hub's inbound-server port: HUBZOID_INBOUND_PORT from its own .env,
    else the default. Resolved with the inbound module's own helper so gateway
    and inbound agree on the port."""
    from .inbound.run import inbound_port

    return inbound_port({"HUBZOID_INBOUND_PORT": _own_env_value(hub_dir, "HUBZOID_INBOUND_PORT")})


def _bridge_model_label(fm_name: str | None, hub_dir: Path) -> str:
    """The model id THE BRIDGE will report at /v1/models, derived with the
    bridge's own functions (loaders.agents name fallback + server slugify) so
    the manifest's model id and the live model can never drift apart."""
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
