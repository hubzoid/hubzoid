"""Build surface configs from the environment, and report missing tokens.

Mirrors ``hubzoid.slack.env``: a surface is enabled only when its tokens are
present, and a missing token yields a clear list so the CLI can soft-warn and
skip that surface without crashing the hub.
"""
from __future__ import annotations

from typing import Mapping

from .harness import TelegramConfig, WhatsAppConfig
# Re-exported so callers have one import site for every surface's env helpers.
from .webhook import (  # noqa: F401
    missing_webhook_vars,
    webhook_config_from_env,
)

_WHATSAPP_VARS = (
    "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
)
_TELEGRAM_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
)


def missing_whatsapp_vars(env: Mapping[str, str]) -> "list[str]":
    return [v for v in _WHATSAPP_VARS if not (env.get(v) or "").strip()]


def missing_telegram_vars(env: Mapping[str, str]) -> "list[str]":
    return [v for v in _TELEGRAM_VARS if not (env.get(v) or "").strip()]


def whatsapp_config_from_env(env: Mapping[str, str]) -> "WhatsAppConfig | None":
    if missing_whatsapp_vars(env):
        return None
    return WhatsAppConfig(
        verify_token=env["WHATSAPP_VERIFY_TOKEN"].strip(),
        app_secret=env["WHATSAPP_APP_SECRET"].strip(),
        token=env["WHATSAPP_TOKEN"].strip(),
        phone_number_id=env["WHATSAPP_PHONE_NUMBER_ID"].strip(),
    )


def telegram_config_from_env(env: Mapping[str, str], *, bindings) -> "TelegramConfig | None":
    if missing_telegram_vars(env):
        return None
    return TelegramConfig(
        secret_token=env["TELEGRAM_WEBHOOK_SECRET"].strip(),
        bot_token=env["TELEGRAM_BOT_TOKEN"].strip(),
        bindings=bindings,
    )
