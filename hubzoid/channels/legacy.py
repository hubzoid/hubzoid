"""The 1.x channels, turned on by a file instead of a flag.

The Slack assistant app (Socket Mode, ``hubzoid.slack``) and the inbound
server for WhatsApp, Telegram and generic webhooks (``hubzoid.inbound``) run
as their hub's own processes, started and restarted with it by the
supervisor. Each is on when the hub has a file for it in ``channels/``::

    # channels/slack-assistant.yaml
    version: 1
    id: slack-assistant
    provider: slack_assistant    # SLACK_BOT_TOKEN, SLACK_APP_TOKEN in .env
    enabled: true

``provider: whatsapp_inbound`` (``WHATSAPP_*``), ``telegram``
(``TELEGRAM_*``) and ``webhook`` (``WEBHOOK_INBOUND_*``) do the same. Their
secrets stay in ``.env``; a file whose secrets are missing leaves that channel
off and says which are missing. 2.0 connections (``hubzoid.channels.config``)
never see these files.

For one release, a hub whose ``.env`` has a 1.x channel's settings but no file
still starts it, with a notice naming the file to add.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

#: provider -> (process, surface)
PROVIDERS = {
    "slack_assistant": ("slack", "slack"),
    "whatsapp_inbound": ("inbound", "whatsapp"),
    "telegram": ("inbound", "telegram"),
    "webhook": ("inbound", "webhook"),
}
_KEYS = {"version", "id", "provider", "enabled"}
#: the file each provider is suggested as
SUGGESTED = {"slack_assistant": "slack-assistant", "whatsapp_inbound": "whatsapp", "telegram": "telegram",
             "webhook": "webhook"}


@dataclass
class Plan:
    """What a hub's 1.x channels need: which processes, which inbound
    surfaces, and what to tell the operator."""
    slack: bool = False
    inbound: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def provider_of(path: Path) -> str | None:
    """A channel file's provider, read cheaply; None when unreadable."""
    import yaml

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return str(data.get("provider")) if isinstance(data, dict) and data.get("provider") else None


def is_legacy(path: Path) -> bool:
    return provider_of(path) in PROVIDERS


def _missing(provider: str, env: Mapping[str, str]) -> list[str]:
    from ..inbound.env import missing_telegram_vars, missing_webhook_vars, missing_whatsapp_vars

    if provider == "slack_assistant":
        from ..slack.env import EnvError, validate_env

        try:
            validate_env(env)
        except EnvError as exc:
            return [str(exc)]
        return []
    return {"whatsapp_inbound": missing_whatsapp_vars, "telegram": missing_telegram_vars,
            "webhook": missing_webhook_vars}[provider](env)


def plan(hub_dir: Path, env: Mapping[str, str], *, two_point_oh_slack: bool | None = None) -> Plan:
    """Which 1.x channel processes this hub runs, from its files (and, for
    one release, from its .env alone)."""
    from .. import _fs
    from ..control import yamlfile

    out = Plan()
    seen: set[str] = set()
    folder = _fs.resolve_bucket(Path(hub_dir), "channels") or Path(hub_dir) / "channels"
    paths = sorted(p for p in folder.iterdir() if p.suffix in (".yaml", ".yml")) if folder.is_dir() else []
    for path in paths:
        provider = provider_of(path)
        if provider not in PROVIDERS:
            continue
        rel = f"{Path(hub_dir).name}/{path.relative_to(hub_dir)}"
        try:
            data = yamlfile.load(path).data or {}
        except yamlfile.YamlError as exc:
            out.problems.append(f"{rel}: {exc.message}")
            continue
        unknown = sorted(set(data) - _KEYS)
        if unknown or data.get("version") != 1 or not isinstance(data.get("enabled", False), bool):
            out.problems.append(f"{rel}: a 1.x channel file has version: 1, id, provider and enabled "
                                f"(true or false)" + (f"; unknown key {unknown[0]!r}" if unknown else ""))
            continue
        seen.add(provider)
        if not data.get("enabled", False):
            continue
        missing = _missing(provider, env)
        if missing:
            out.problems.append(f"{rel}: off, its settings are missing from .env: {', '.join(missing)}")
            continue
        _turn_on(out, provider)
    if two_point_oh_slack is None:
        two_point_oh_slack = any(provider_of(p) == "slack" for p in paths)
    for provider in PROVIDERS:
        if provider in seen or _missing(provider, env):
            continue
        if provider == "slack_assistant" and two_point_oh_slack:
            continue  # those tokens belong to a 2.0 Slack connection
        _turn_on(out, provider)
        out.notes.append(f"{Path(hub_dir).name}/.env has the {provider} settings but no channel file. It "
                         f"starts for this release; add channels/{SUGGESTED[provider]}.yaml "
                         f"(provider: {provider}, enabled: true) to keep it on.")
    return out


def _turn_on(out: Plan, provider: str) -> None:
    process, surface = PROVIDERS[provider]
    if process == "slack":
        out.slack = True
    elif surface not in out.inbound:
        out.inbound.append(surface)


def file_text(provider: str) -> str:
    """The file that turns a 1.x channel on."""
    return (f"version: 1\nid: {SUGGESTED[provider]}\nprovider: {provider}\nenabled: true\n")
