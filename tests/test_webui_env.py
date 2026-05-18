"""Tests for the env-var set hubzoid passes to the Open WebUI subprocess.

We don't actually launch OWUI here; we patch subprocess.Popen and inspect
the env it would have been called with.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hubzoid import webui


@pytest.fixture
def captured_env(tmp_path, monkeypatch):
    """Patch the OWUI binary lookup and Popen; return the env dict that would
    have been passed to OWUI."""
    monkeypatch.setattr(webui, "_find_binary", lambda: "/fake/open-webui")
    captured: dict[str, str] = {}

    def fake_popen(cmd, env=None, stdout=None, stderr=None):
        captured.update(env or {})
        proc = MagicMock()
        proc._log_path = tmp_path / "log"
        return proc

    with patch("hubzoid.webui.subprocess.Popen", fake_popen):
        yield captured


def _start(captured_env, tmp_path, **overrides):
    """Call webui.start with sensible defaults; return the captured env."""
    hub = tmp_path / "my-hub"
    hub.mkdir(exist_ok=True)
    webui.start(
        hub_dir=hub,
        bridge_port=8000,
        ui_port=3080,
        api_key="dev",
        model_label="my-hub",
        webui_name="My Hub",
        **overrides,
    )
    return captured_env


# ---------------------------------------------------------------------------
# Off-flags
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("flag", [
    "ENABLE_COMMUNITY_SHARING",
    "ENABLE_DIRECT_CONNECTIONS",
    "ENABLE_EVALUATION_ARENA_MODELS",
    "ENABLE_NOTES",
    "ENABLE_CHANNELS",
    "ENABLE_CODE_INTERPRETER",
    "ENABLE_IMAGE_GENERATION",
    "ENABLE_RAG_WEB_SEARCH",
    "ENABLE_USER_WEBHOOKS",
    "ENABLE_TAGS_GENERATION",
    "ENABLE_API_KEY",
    "ENABLE_VERSION_UPDATE_CHECK",
    "ENABLE_MEMORY",
    "ENABLE_OLLAMA_API",
    "SHOW_ADMIN_DETAILS",
    "ENABLE_PERSISTENT_CONFIG",
    "USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS",
    "USER_PERMISSIONS_WORKSPACE_TOOLS_ACCESS",
    "USER_PERMISSIONS_WORKSPACE_FUNCTIONS_ACCESS",
    "USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS",
    "USER_PERMISSIONS_WORKSPACE_PROMPTS_ACCESS",
])
def test_strip_flags_default_off(captured_env, tmp_path, flag, monkeypatch):
    monkeypatch.delenv(flag, raising=False)
    env = _start(captured_env, tmp_path)
    assert env[flag] == "False"


# ---------------------------------------------------------------------------
# On-flags
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("flag", [
    "ENABLE_MESSAGE_RATING",
    "ENABLE_TITLE_GENERATION",
    "ENABLE_ADMIN_EXPORT",
    "ENABLE_FOLLOW_UP_GENERATION",
])
def test_ux_flags_default_on(captured_env, tmp_path, flag, monkeypatch):
    monkeypatch.delenv(flag, raising=False)
    env = _start(captured_env, tmp_path)
    assert env[flag] == "True"


# ---------------------------------------------------------------------------
# Operator override wins
# ---------------------------------------------------------------------------
def test_operator_env_overrides_default(captured_env, tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_CODE_INTERPRETER", "True")
    env = _start(captured_env, tmp_path)
    assert env["ENABLE_CODE_INTERPRETER"] == "True"


def test_operator_can_disable_kept_on_flag(captured_env, tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_FOLLOW_UP_GENERATION", "False")
    env = _start(captured_env, tmp_path)
    assert env["ENABLE_FOLLOW_UP_GENERATION"] == "False"


# ---------------------------------------------------------------------------
# Wiring + branding cascade
# ---------------------------------------------------------------------------
def test_wiring_uses_provided_ports(captured_env, tmp_path):
    env = _start(captured_env, tmp_path)
    assert env["OPENAI_API_BASE_URL"] == "http://127.0.0.1:8000/v1"
    assert env["OPENAI_API_KEY"] == "dev"
    assert env["DEFAULT_MODELS"] == "my-hub"


def test_webui_name_passed_through(captured_env, tmp_path):
    env = _start(captured_env, tmp_path)
    assert env["WEBUI_NAME"] == "My Hub"


def test_response_watermark_defaults_to_hub_name(captured_env, tmp_path, monkeypatch):
    monkeypatch.delenv("RESPONSE_WATERMARK", raising=False)
    env = _start(captured_env, tmp_path)
    assert env["RESPONSE_WATERMARK"] == "my-hub"


def test_response_watermark_operator_override(captured_env, tmp_path, monkeypatch):
    monkeypatch.setenv("RESPONSE_WATERMARK", "ACME")
    env = _start(captured_env, tmp_path)
    assert env["RESPONSE_WATERMARK"] == "ACME"


# ---------------------------------------------------------------------------
# Suggestions serialization (DEFAULT_PROMPT_SUGGESTIONS)
# ---------------------------------------------------------------------------
def test_suggestions_serialized_as_json_array_of_objects(captured_env, tmp_path, monkeypatch):
    monkeypatch.delenv("DEFAULT_PROMPT_SUGGESTIONS", raising=False)
    env = _start(captured_env, tmp_path, suggestions=["foo", "bar"])
    payload = json.loads(env["DEFAULT_PROMPT_SUGGESTIONS"])
    assert payload == [{"content": "foo"}, {"content": "bar"}]


def test_suggestions_omitted_when_empty(captured_env, tmp_path, monkeypatch):
    monkeypatch.delenv("DEFAULT_PROMPT_SUGGESTIONS", raising=False)
    env = _start(captured_env, tmp_path, suggestions=[])
    assert "DEFAULT_PROMPT_SUGGESTIONS" not in env


def test_suggestions_filters_empty_strings(captured_env, tmp_path, monkeypatch):
    monkeypatch.delenv("DEFAULT_PROMPT_SUGGESTIONS", raising=False)
    env = _start(captured_env, tmp_path, suggestions=["x", "", "y"])
    payload = json.loads(env["DEFAULT_PROMPT_SUGGESTIONS"])
    assert payload == [{"content": "x"}, {"content": "y"}]
