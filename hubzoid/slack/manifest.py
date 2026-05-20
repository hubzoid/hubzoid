"""Generate a Slack App Manifest for a hub.

The manifest is pasted into https://api.slack.com/apps -> "Create New App"
-> "From a manifest" to create the bot. Slack's UI accepts JSON or YAML;
JSON is the default here because it survives copy/paste through terminals
without indentation getting mangled.

We pre-fill name, description, and suggested prompts from the hub's
AGENTS.md frontmatter so the operator does not have to copy fields by hand.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml

from ..loaders import agents as agents_loader


ManifestFormat = Literal["json", "yaml"]


# Bot scopes needed for the chat surface, in alphabetic order so the diff
# stays readable when we add or drop one.
#
# Why each "*:history" scope: the adapter calls conversations.replies to
# fetch the surrounding thread before forwarding to the bridge. Slack scopes
# that call per channel type — so we need one history scope per place the
# bot might be invited (channels, private groups, multi-person DMs, DMs).
_BOT_SCOPES = [
    "app_mentions:read",
    "assistant:write",
    "channels:history",   # public channel threads (where bot is invited)
    "chat:write",
    "chat:write.public",
    "groups:history",     # private channel threads
    "im:history",         # DMs
    "im:read",
    "im:write",
    "mpim:history",       # group DM threads
    "users:read",
]

_BOT_EVENTS = [
    "app_mention",
    "assistant_thread_context_changed",
    "assistant_thread_started",
    "message.im",
]


def manifest_for_hub(hub_dir: Path, *, format: ManifestFormat = "json") -> str:
    """Return a Slack App Manifest as a string for `hub_dir`.

    `format`: "json" (default — clean to copy from a terminal) or "yaml".

    Reads `<hub>/AGENTS.md` for name, description, and suggestions. Falls back
    to the hub folder name if AGENTS.md is malformed.
    """
    name, description, suggestions = _read_agent_meta(hub_dir)

    assistant_view: dict[str, Any] = {
        "assistant_description": description,
    }
    if suggestions:
        assistant_view["suggested_prompts"] = [
            {"title": s, "message": s} for s in suggestions[:4]
        ]

    manifest: dict[str, Any] = {
        "display_information": {
            "name": name,
            "description": description,
            "background_color": "#0b0d12",
        },
        "features": {
            "bot_user": {
                "display_name": name,
                "always_online": True,
            },
            "assistant_view": assistant_view,
            # Without this the "Messages" tab in the bot's profile is off
            # by default and DMs get rejected with "Sending messages to
            # this app has been turned off."
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
        },
        "oauth_config": {
            "scopes": {"bot": list(_BOT_SCOPES)},
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": list(_BOT_EVENTS),
            },
            "interactivity": {"is_enabled": True},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }
    if format == "yaml":
        return yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False)
    return json.dumps(manifest, indent=2, ensure_ascii=False)


def _read_agent_meta(hub_dir: Path) -> tuple[str, str, list[str]]:
    try:
        main = agents_loader.load_main(hub_dir)
        return main.spec.name, main.spec.description, list(main.spec.suggestions)
    except Exception:  # noqa: BLE001
        return hub_dir.name, f"Hubzoid agent: {hub_dir.name}", []
