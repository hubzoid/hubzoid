"""Top-level entry: serve the inbound webhook app for a hub.

Reads the hub's settings and env, builds the identity resolver and whichever
surface configs are present (WhatsApp / Telegram), and serves the harness on a
loopback port. The public edge routes ``/webhooks/*`` to this port — the app
itself never binds the public interface, matching the bridge's posture.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .. import settings as settingslib
from ..access.resolver import load_resolver
from ..telegram.enrollment import Bindings
from .env import telegram_config_from_env, whatsapp_config_from_env
from .harness import Messages, build_app
from .history import DEFAULT_MAX_MESSAGES
from .webhook import webhook_config_from_env

log = logging.getLogger("hubzoid.inbound")

DEFAULT_INBOUND_PORT = 8100


def inbound_port(env=None) -> int:
    env = env if env is not None else os.environ
    try:
        return int((env.get("HUBZOID_INBOUND_PORT") or "").strip() or DEFAULT_INBOUND_PORT)
    except ValueError:
        return DEFAULT_INBOUND_PORT


def _slugify(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in str(text).strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "hub"


def hub_slug(hub_dir, env=None) -> str:
    """The URL slug this hub's webhooks are namespaced under: ``HUBZOID_HUB_SLUG``
    if the operator pinned one, else the slugified folder name.

    The gateway edge and this inbound app must agree on the slug or the route
    404s. Both apply this same rule, so distinct folder names need no config; set
    ``HUBZOID_HUB_SLUG`` only when the gateway had to de-dup a slug collision
    (two hubs with the same folder basename)."""
    env = env if env is not None else os.environ
    pinned = (env.get("HUBZOID_HUB_SLUG") or "").strip()
    return _slugify(pinned) if pinned else _slugify(Path(hub_dir).name)


def build_app_for_hub(hub_dir, env=None):
    """Build the Starlette app for a hub, or None if no surface is configured."""
    env = env if env is not None else os.environ
    hub_dir = Path(hub_dir)
    settings = settingslib.load(hub_dir)
    resolver = load_resolver(hub_dir)
    bindings = Bindings(hub_dir / ".inbound" / "telegram-bindings")

    wa = whatsapp_config_from_env(env)
    tg = telegram_config_from_env(env, bindings=bindings)
    wh = webhook_config_from_env(env, hub_dir=hub_dir)
    if wa is None and tg is None and wh is None:
        return None
    # Streaming (edit-in-place) is ON by default; set TELEGRAM_STREAM=false to disable.
    if tg is not None and settingslib.truthy(env.get("TELEGRAM_STREAM", "true")):
        tg.stream = True

    if resolver is None:
        log.warning(
            "inbound: no identity/access.{csv,py} found — every sender is "
            "unknown and will be rejected. Add a roster to let coordinators in."
        )

    model = settings.model_label or _model_label(hub_dir)
    bridge_url = f"http://127.0.0.1:{settings.bridge_port}/v1"
    try:
        history_max = int((env.get("INBOUND_HISTORY_MAX") or "").strip() or DEFAULT_MAX_MESSAGES)
    except ValueError:
        history_max = DEFAULT_MAX_MESSAGES
    try:
        ttl_days = float((env.get("INBOUND_HISTORY_TTL_DAYS") or "").strip() or 0)
    except ValueError:
        ttl_days = 0.0
    history_ttl_seconds = ttl_days * 86400 if ttl_days > 0 else None
    try:
        stream_interval = float((env.get("INBOUND_STREAM_INTERVAL") or "").strip() or 1.0)
    except ValueError:
        stream_interval = 1.0
    stream_interval = max(0.8, stream_interval)  # floor: below ~0.8s risks Telegram 429s

    return build_app(
        hub_dir=hub_dir, bridge_url=bridge_url, api_key=settings.first_api_key,
        model=model, resolver=resolver, slug=hub_slug(hub_dir, env),
        whatsapp=wa, telegram=tg, webhook=wh,
        messages=Messages.from_env(env), history_max=history_max,
        history_ttl_seconds=history_ttl_seconds, stream_interval=stream_interval,
        max_upload_bytes=settings.max_upload_bytes,
    )


def run(hub_dir, env=None, port=None) -> int:
    """Serve the inbound app (blocking). Returns a CLI exit code."""
    env = env if env is not None else os.environ
    app = build_app_for_hub(hub_dir, env)
    if app is None:
        log.error(
            "inbound: no WhatsApp or Telegram tokens in .env — nothing to serve. "
            "Set WHATSAPP_* and/or TELEGRAM_* (see docs)."
        )
        return 1

    import uvicorn

    bind_port = port or inbound_port(env)
    slug = hub_slug(hub_dir, env)
    log.info("hubzoid inbound starting on 127.0.0.1:%s (hub=%s, public path /webhooks/%s/*)",
             bind_port, Path(hub_dir).name, slug)
    uvicorn.run(app, host="127.0.0.1", port=bind_port, log_level="info")
    return 0


def _model_label(hub_dir) -> str:
    try:
        from ..loaders import agents as agents_loader

        name = agents_loader.load_main(hub_dir).spec.name
    except Exception:  # noqa: BLE001
        name = Path(hub_dir).name
    return _slugify(name)


def _slugify(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (text or "").strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "agent"
