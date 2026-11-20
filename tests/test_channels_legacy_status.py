"""The 1.x channels show in `hubzoid channels list`, and a 2.0 Slack file that
takes over the 1.x Slack app's tokens says so instead of turning it off silently."""
from __future__ import annotations

from typer.testing import CliRunner

from hubzoid.channels import legacy

# Shapes only: the 1.x Slack check reads the prefixes, never calls Slack.
SLACK_ENV = {"SLACK_BOT_TOKEN": "xoxb-test-only", "SLACK_APP_TOKEN": "xapp-test-only"}  # gitleaks:allow


def test_a_2_0_slack_file_turning_off_the_1x_app_says_so(tmp_path):
    hub = tmp_path / "ops"
    (hub / "channels").mkdir(parents=True)
    (hub / "channels" / "slack-main.yaml").write_text("version: 1\nid: slack-main\nprovider: slack\nenabled: false\n")
    plan = legacy.plan(hub, SLACK_ENV)
    assert plan.slack is False and any("1.x Slack app is off" in n for n in plan.notes)


def test_channels_list_shows_1x_channels(tmp_path, monkeypatch):
    from hubzoid.cli import app

    import os

    for key in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB",
                *(k for k in os.environ if k.startswith(("SLACK_", "WHATSAPP_", "TELEGRAM_")))):
        monkeypatch.delenv(key, raising=False)
    hub = tmp_path / "ops"
    (hub / "channels").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: Ops\ndescription: The ops agent.\n---\nOps.\n")
    (hub / ".env").write_text("WEBHOOK_INBOUND_SECRET=s3cr3t-value-long\nWEBHOOK_INBOUND_NAME=alerts\n")  # gitleaks:allow
    (hub / "channels" / "webhook.yaml").write_text("version: 1\nid: webhook\nprovider: webhook\nenabled: true\n")
    result = CliRunner().invoke(app, ["channels", "list", str(hub)])
    out = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "1.x channels running: webhook" in out and "No channels." not in out
