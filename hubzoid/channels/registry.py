"""Which adapter serves which provider.

Built-in providers load lazily from their own module, so a hub without a
Slack connection never imports Slack's SDK. A test or a hub extension can
register its own adapter.
"""
from __future__ import annotations

import importlib

from .errors import ChannelError
from .protocol import Adapter

#: provider name -> "module:attribute" of a built-in adapter class
BUILTIN: dict[str, str] = {
    "slack": "hubzoid.channels.slack.adapter:SlackAdapter",
    "github": "hubzoid.channels.github.adapter:GitHubAdapter",
    "linear": "hubzoid.channels.linear.adapter:LinearAdapter",
    "email": "hubzoid.channels.email.adapter:EmailAdapter",
    "whatsapp_cloud": "hubzoid.channels.whatsapp.adapter:WhatsAppAdapter",
}

_registered: dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    if not adapter.provider:
        raise ValueError("an adapter needs a provider name")
    _registered[adapter.provider] = adapter
    return adapter


def unregister(provider: str) -> None:
    _registered.pop(provider, None)


def known() -> list[str]:
    names = set(_registered)
    for name, target in BUILTIN.items():
        if _importable(target):
            names.add(name)
    return sorted(names)


def _importable(target: str) -> bool:
    module = target.split(":", 1)[0]
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def get(provider: str) -> Adapter:
    adapter = _registered.get(provider)
    if adapter is not None:
        return adapter
    target = BUILTIN.get(provider)
    if target is None or not _importable(target):
        raise ChannelError("unknown_provider",
                           f"No channel provider named {provider!r}. Known providers: "
                           f"{', '.join(known()) or 'none'}.", status=422)
    module, attr = target.split(":", 1)
    adapter = getattr(importlib.import_module(module), attr)()
    _registered[provider] = adapter
    return adapter
