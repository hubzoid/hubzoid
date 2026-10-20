"""The Slack app manifest for a connection, generated from its applied file.

The builder creates the app in their own Slack workspace from this manifest.
It holds no credentials. Permissions are the smallest set the channel uses:
no ``chat:write.public`` (the bot answers only where it was invited) and no
email access (people are linked by Slack user id).
"""
from __future__ import annotations

import json
from typing import Any

import yaml

from .adapter import BASE_SCOPES


def build(conn) -> dict[str, Any]:
    from ...slack.manifest import _read_agent_meta
    from ..doctor import webhook_url

    cfg = conn.settings
    name, description, suggestions = _read_agent_meta(conn.hub_dir)
    scopes = sorted(set(BASE_SCOPES) | ({"files:read"} if cfg.files_enabled else set()) | {"assistant:write"})
    events = ["app_mention", "message.channels", "message.groups", "message.im", "message.mpim"]
    settings: dict[str, Any] = {
        "event_subscriptions": {"bot_events": events},
        "interactivity": {"is_enabled": True},
        "org_deploy_enabled": False,
        "socket_mode_enabled": cfg.transport == "socket_mode",
        "token_rotation_enabled": False,
    }
    if cfg.transport == "http":
        url = webhook_url(conn) or "https://YOUR-PUBLIC-URL/api/hubs/<hub>/channels/<id>/webhook"
        settings["event_subscriptions"]["request_url"] = url
        settings["interactivity"]["request_url"] = url
    assistant = {"assistant_description": description[:144]}
    if suggestions:
        assistant["suggested_prompts"] = [{"title": s[:75], "message": s} for s in suggestions[:4]]
    return {
        "display_information": {"name": name[:35], "description": description[:144],
                                "background_color": "#0b0d12"},
        "features": {
            "bot_user": {"display_name": name[:80], "always_online": True},
            "assistant_view": assistant,
            "app_home": {"home_tab_enabled": False, "messages_tab_enabled": True,
                         "messages_tab_read_only_enabled": False},
        },
        "oauth_config": {"scopes": {"bot": scopes}},
        "settings": settings,
    }


def render(conn, fmt: str = "json") -> str:
    data = build(conn)
    if fmt == "yaml":
        return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    return json.dumps(data, indent=2, ensure_ascii=False)
