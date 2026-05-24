"""Tests for the Slack App Manifest generator + env validation + systemd unit."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hubzoid.slack.env import EnvError, validate_env
from hubzoid.slack.manifest import manifest_for_hub
from hubzoid.slack.service import systemd_unit_for_hub


FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


# ---------------------------------------------------------------------------
# manifest_for_hub
# ---------------------------------------------------------------------------
def test_manifest_for_hub_uses_main_agent_name(tmp_path):
    hub = tmp_path / "alpha"
    hub.mkdir()
    (hub / "AGENTS.md").write_text(
        "---\nname: alpha-bot\ndescription: alpha description here\n---\n\nbody\n"
    )
    out = yaml.safe_load(manifest_for_hub(hub))
    assert out["display_information"]["name"] == "alpha-bot"
    assert "alpha description" in out["display_information"]["description"]
    assert out["features"]["bot_user"]["display_name"] == "alpha-bot"


def test_manifest_for_hub_includes_suggestions(tmp_path):
    hub = tmp_path / "alpha"
    hub.mkdir()
    (hub / "AGENTS.md").write_text(
        "---\nname: a\ndescription: d\nsuggestions:\n  - ask 1\n  - ask 2\n  - ask 3\n---\n\nbody\n"
    )
    out = yaml.safe_load(manifest_for_hub(hub))
    prompts = out["features"]["assistant_view"]["suggested_prompts"]
    titles = [p["title"] for p in prompts]
    assert titles == ["ask 1", "ask 2", "ask 3"]


def test_manifest_for_hub_no_suggestions_omits_assistant_prompts(tmp_path):
    hub = tmp_path / "alpha"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    out = yaml.safe_load(manifest_for_hub(hub))
    assert "suggested_prompts" not in out["features"].get("assistant_view", {})


def test_manifest_for_hub_required_scopes_and_events(tmp_path):
    hub = tmp_path / "alpha"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    out = yaml.safe_load(manifest_for_hub(hub))
    scopes = set(out["oauth_config"]["scopes"]["bot"])
    events = set(out["settings"]["event_subscriptions"]["bot_events"])
    assert {"app_mentions:read", "chat:write", "im:history", "assistant:write"} <= scopes
    assert {"app_mention", "message.im", "assistant_thread_started"} <= events
    assert out["settings"]["socket_mode_enabled"] is True


def test_manifest_for_hub_history_scopes_for_all_channel_types(tmp_path):
    """The adapter calls conversations.replies; Slack rejects without per-channel
    history scopes. Regression test for missing_scope errors we hit in real
    workspaces (channels:history, groups:history, mpim:history)."""
    hub = tmp_path / "alpha"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    scopes = set(yaml.safe_load(manifest_for_hub(hub))["oauth_config"]["scopes"]["bot"])
    assert {
        "channels:history",
        "groups:history",
        "im:history",
        "mpim:history",
    } <= scopes


def test_manifest_for_hub_includes_files_read_scope(tmp_path):
    """Required by `download_message_files` to call files.info on Slack
    attachments. Without it, every user-uploaded file in a thread fails
    with `missing_scope` and the agent never sees the attachment."""
    hub = tmp_path / "alpha"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    scopes = set(yaml.safe_load(manifest_for_hub(hub))["oauth_config"]["scopes"]["bot"])
    assert "files:read" in scopes


def test_manifest_for_hub_enables_messages_tab(tmp_path):
    """Without this the 'Sending messages to this app has been turned off'
    error blocks DMs entirely. Regression test."""
    hub = tmp_path / "alpha"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: a\ndescription: d\n---\n\nbody\n")
    out = yaml.safe_load(manifest_for_hub(hub))
    app_home = out["features"]["app_home"]
    assert app_home["messages_tab_enabled"] is True
    assert app_home["messages_tab_read_only_enabled"] is False


def test_manifest_for_hub_uses_minimal_fixture():
    """Sanity check that the existing test fixture round-trips."""
    out = yaml.safe_load(manifest_for_hub(MINIMAL))
    assert out["display_information"]["name"] == "testbot"


def test_manifest_for_hub_defaults_to_json():
    """Default format is JSON — terminal-friendly, no indentation gotchas."""
    raw = manifest_for_hub(MINIMAL)
    # JSON output starts with '{', YAML output starts with a key.
    assert raw.lstrip().startswith("{")
    parsed = json.loads(raw)
    assert parsed["display_information"]["name"] == "testbot"


def test_manifest_for_hub_yaml_format_opt_in():
    raw = manifest_for_hub(MINIMAL, format="yaml")
    assert not raw.lstrip().startswith("{")
    parsed = yaml.safe_load(raw)
    assert parsed["display_information"]["name"] == "testbot"


# ---------------------------------------------------------------------------
# validate_env
# ---------------------------------------------------------------------------
def test_validate_env_passes_with_both_tokens():
    validate_env({"SLACK_BOT_TOKEN": "xoxb-abc", "SLACK_APP_TOKEN": "xapp-abc"})


def test_validate_env_rejects_missing_bot_token():
    with pytest.raises(EnvError) as exc:
        validate_env({"SLACK_APP_TOKEN": "xapp-abc"})
    assert "SLACK_BOT_TOKEN" in str(exc.value)
    assert "hubzoid slack manifest" in str(exc.value)


def test_validate_env_rejects_missing_app_token():
    with pytest.raises(EnvError) as exc:
        validate_env({"SLACK_BOT_TOKEN": "xoxb-abc"})
    assert "SLACK_APP_TOKEN" in str(exc.value)


def test_validate_env_rejects_blank_tokens():
    with pytest.raises(EnvError):
        validate_env({"SLACK_BOT_TOKEN": "  ", "SLACK_APP_TOKEN": ""})


def test_validate_env_rejects_wrong_bot_token_prefix():
    """Catches a common copy-paste error: pasting the app token into both slots."""
    with pytest.raises(EnvError) as exc:
        validate_env({"SLACK_BOT_TOKEN": "xapp-abc", "SLACK_APP_TOKEN": "xapp-abc"})
    assert "xoxb-" in str(exc.value)


def test_validate_env_rejects_wrong_app_token_prefix():
    with pytest.raises(EnvError) as exc:
        validate_env({"SLACK_BOT_TOKEN": "xoxb-abc", "SLACK_APP_TOKEN": "xoxb-abc"})
    assert "xapp-" in str(exc.value)


# ---------------------------------------------------------------------------
# should_start_slack — soft-warn decision for `hubzoid run --slack`
# ---------------------------------------------------------------------------
def test_should_start_slack_false_when_flag_not_set():
    from hubzoid.slack.env import should_start_slack

    ok, warn = should_start_slack(want_slack=False, env={})
    assert ok is False
    assert warn is None  # flag not set -> silent, no warning


def test_should_start_slack_true_when_flag_and_tokens_present():
    from hubzoid.slack.env import should_start_slack

    ok, warn = should_start_slack(
        want_slack=True,
        env={"SLACK_BOT_TOKEN": "xoxb-abc", "SLACK_APP_TOKEN": "xapp-abc"},
    )
    assert ok is True
    assert warn is None


def test_should_start_slack_soft_warns_when_flag_but_tokens_missing():
    """The whole point of soft-warn: requesting --slack without tokens must
    not crash the parent; just return False + a clear warning string."""
    from hubzoid.slack.env import should_start_slack

    ok, warn = should_start_slack(want_slack=True, env={})
    assert ok is False
    assert warn is not None
    assert "SLACK_BOT_TOKEN" in warn
    assert "hubzoid slack manifest" in warn  # actionable pointer


def test_should_start_slack_soft_warns_on_swapped_tokens():
    from hubzoid.slack.env import should_start_slack

    ok, warn = should_start_slack(
        want_slack=True,
        env={"SLACK_BOT_TOKEN": "xapp-bot", "SLACK_APP_TOKEN": "xapp-app"},
    )
    assert ok is False
    assert warn is not None
    assert "xoxb-" in warn


# ---------------------------------------------------------------------------
# systemd_unit_for_hub
# ---------------------------------------------------------------------------
def test_systemd_unit_includes_hub_path_and_after_run():
    unit = systemd_unit_for_hub(
        hub_dir=Path("/srv/hubzoid/devops-agent"),
        python_path=Path("/srv/hubzoid/.venv/bin/python"),
        user="hubzoid",
    )
    assert "/srv/hubzoid/devops-agent" in unit
    assert "/srv/hubzoid/.venv/bin/python" in unit
    assert "User=hubzoid" in unit
    assert "Restart=always" in unit
    # Slack adapter must come up after the bridge is running.
    assert "After=hubzoid@" in unit or "Requires=hubzoid@" in unit
    assert "-m hubzoid slack" in unit or "hubzoid slack" in unit
