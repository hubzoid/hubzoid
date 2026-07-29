"""Build surface configs from environment, and report missing tokens so the CLI
can soft-warn and skip a surface (like should_start_slack) rather than crash."""
from hubzoid.inbound.env import (
    missing_telegram_vars,
    missing_whatsapp_vars,
    telegram_config_from_env,
    whatsapp_config_from_env,
)


def test_whatsapp_config_built_when_all_vars_present():
    env = {"WHATSAPP_VERIFY_TOKEN": "v", "WHATSAPP_APP_SECRET": "s",
           "WHATSAPP_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "p"}
    cfg = whatsapp_config_from_env(env)
    assert cfg.verify_token == "v"
    assert cfg.app_secret == "s"
    assert cfg.token == "t"
    assert cfg.phone_number_id == "p"


def test_whatsapp_config_none_when_incomplete():
    assert whatsapp_config_from_env({"WHATSAPP_TOKEN": "t"}) is None


def test_missing_whatsapp_vars_lists_the_gaps():
    missing = missing_whatsapp_vars({"WHATSAPP_TOKEN": "t"})
    assert "WHATSAPP_VERIFY_TOKEN" in missing
    assert "WHATSAPP_APP_SECRET" in missing
    assert "WHATSAPP_PHONE_NUMBER_ID" in missing
    assert "WHATSAPP_TOKEN" not in missing


def test_telegram_config_built_when_all_vars_present():
    env = {"TELEGRAM_BOT_TOKEN": "b", "TELEGRAM_WEBHOOK_SECRET": "s"}
    cfg = telegram_config_from_env(env, bindings="BINDINGS")
    assert cfg.bot_token == "b"
    assert cfg.secret_token == "s"
    assert cfg.bindings == "BINDINGS"


def test_telegram_config_none_when_incomplete():
    assert telegram_config_from_env({"TELEGRAM_BOT_TOKEN": "b"}, bindings=None) is None


def test_missing_telegram_vars_lists_the_gaps():
    assert missing_telegram_vars({}) == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET"]
