"""The 1.x channels (Slack assistant, inbound WhatsApp, Telegram, webhook) show
in `hubzoid channels list` next to the 2.0 connections, and a 2.0 Slack file
that takes over the 1.x Slack app's tokens says so instead of turning it off
without a word."""
from __future__ import annotations

from typer.testing import CliRunner

from hubzoid.channels import legacy

# Shapes only: the 1.x Slack check reads the prefixes, never calls Slack.
SLACK_ENV = {"SLACK_BOT_TOKEN": "xoxb-test-only", "SLACK_APP_TOKEN": "xapp-test-only"}  # gitleaks:allow
WEBHOOK_ENV = {"WEBHOOK_INBOUND_SECRET": "s3cr3t-value-long", "WEBHOOK_INBOUND_NAME": "alerts"}  # gitleaks:allow


def _hub(tmp_path):
    hub = tmp_path / "ops"
    (hub / "channels").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: Ops\ndescription: The ops agent.\n---\nOps.\n")
    return hub


def test_a_2_0_slack_file_turning_off_the_1x_app_says_so(tmp_path):
    hub = _hub(tmp_path)
    (hub / "channels" / "slack-main.yaml").write_text("version: 1\nid: slack-main\nprovider: slack\nenabled: false\n")
    plan = legacy.plan(hub, SLACK_ENV)
    assert plan.slack is False
    assert any("1.x Slack app is off" in n for n in plan.notes)
    (row,) = legacy.status(hub, SLACK_ENV)
    assert row["provider"] == "slack_assistant" and row["source"] == ".env" and row["status"] == "off"
    assert "2.0 Slack connection" in row["note"]


def test_status_lists_file_and_env_only_channels(tmp_path):
    hub = _hub(tmp_path)
    (hub / "channels" / "webhook.yaml").write_text("version: 1\nid: webhook\nprovider: webhook\nenabled: true\n")
    rows = {r["provider"]: r for r in legacy.status(hub, {**WEBHOOK_ENV, **SLACK_ENV})}
    assert rows["webhook"]["source"] == "channels/webhook.yaml" and rows["webhook"]["status"] == "on"
    assert rows["slack_assistant"]["source"] == ".env" and rows["slack_assistant"]["status"] == "on"
    assert "channels/slack-assistant.yaml" in rows["slack_assistant"]["note"]   # the file to add


def test_channels_list_shows_1x_channels(tmp_path, monkeypatch):
    from hubzoid.cli import app

    for key in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB"):
        monkeypatch.delenv(key, raising=False)
    hub = _hub(tmp_path)
    (hub / ".env").write_text("".join(f"{k}={v}\n" for k, v in WEBHOOK_ENV.items()))
    (hub / "channels" / "webhook.yaml").write_text("version: 1\nid: webhook\nprovider: webhook\nenabled: true\n")
    result = CliRunner().invoke(app, ["channels", "list", str(hub)])
    out = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "1.x channel webhook (channels/webhook.yaml): on." in out and "No channels." not in out
