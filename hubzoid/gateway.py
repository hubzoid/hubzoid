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
            # Forward the logged-in user's identity to every bridge, exactly as
            # the single-hub path does (webui.start). Without it OWUI never sends
            # X-OpenWebUI-User-Email, so the bridge derives an anonymous identity
            # and every restricted tool is denied ("anonymous") — access control
            # is dead in gateway mode. See hubzoid.server._derive_identity and
            # hubzoid.access.owui_groups.
            "ENABLE_FORWARD_USER_INFO_HEADERS": "true",
        }
        labels = [b.model_label for b in self.backends if b.model_label]
        if labels:
            env["DEFAULT_MODELS"] = labels[0]
        return env

    def edge_routes(self, *, artifact_prefix: str = "/artifacts") -> list[dict]:
        """Per-hub artifact routes for the edge: /b/<slug>/artifacts -> bridge.

        `strip_prefix` removes `/b/<slug>` so the bridge sees its native
        `/artifacts/...` path.
        """
        routes = []
        for b in self.backends:
            base = f"/b/{b.slug}"
            routes.append({
                "prefix": base + artifact_prefix,
                "upstream": f"http://127.0.0.1:{b.bridge_port}",
                "strip_prefix": base,
            })
        return routes

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
        backends.append(GatewayBackend(
            hub_dir=hub_dir,
            slug=slug,
            bridge_port=s.bridge_port,
            api_key=s.first_api_key,
            model_label=s.model_label or _slugify(meta["name"]),
            display_name=meta["name"],
            description=meta["description"],
            suggestions=meta["suggestions"],
            logo=_find_logo(hub_dir),
        ))
    return GatewayPlan(backends=tuple(backends))


def _agent_meta(hub_dir: Path) -> dict:
    """Best-effort hub identity from AGENTS.md frontmatter. Never raises:
    a missing/broken AGENTS.md yields folder-name + empty fields, matching
    the fail-safe posture of everything provisioning-related."""
    from . import frontmatter as fm
    name, description, suggestions = hub_dir.name, "", ()
    try:
        data, _ = fm.read(hub_dir / "AGENTS.md")
        name = str(data.get("name") or hub_dir.name)
        description = str(data.get("description") or "")
        raw = data.get("suggestions") or []
        if isinstance(raw, list):
            suggestions = tuple(str(s) for s in raw if str(s).strip())
    except Exception:  # noqa: BLE001
        pass
    return {"name": name, "description": description, "suggestions": suggestions}


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
