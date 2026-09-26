"""The claude CLI child (and the stdio MCP servers it starts) must not inherit
service secrets, AWS credentials or restricted-tool values, while keeping its
own sign-in and the hub settings MCP servers rely on."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from hubzoid import config_secrets as cs
from hubzoid import settings
from tests._fake_secrets import clean_process_env, install

MINIMAL = Path(__file__).parent / "fixtures" / "minimal_hub"

SCRUBBED = {
    # secret names
    "AWS_SECRET_NAME", "HUBZOID_HUB_SECRET_NAME", "HUBZOID_RESTRICTED_SECRET_NAME",
    # AWS credential material
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_BEARER_TOKEN_BEDROCK",
    # Hubzoid and Open WebUI service secrets
    "BRIDGE_API_KEYS", "HUBZOID_ARTIFACT_SECRET", "HUBZOID_GATEWAY_ADMIN_PASSWORD", "WEBUI_SECRET_KEY",
    "OAUTH_SESSION_TOKEN_ENCRYPTION_KEY", "OAUTH_CLIENT_INFO_ENCRYPTION_KEY",
    "DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB",
}
KEPT = {
    # the claude CLI's own sign-in and settings
    "ANTHROPIC_API_KEY": "sk-ant", "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "ANTHROPIC_BASE_URL": "https://a",
    # selectors, not credentials (a blank AWS_PROFILE breaks boto3 in MCP servers)
    "AWS_PROFILE": "dev", "AWS_REGION": "eu-west-1", "AWS_DEFAULT_REGION": "eu-west-1",
    "AWS_CONFIG_FILE": "/c", "AWS_SHARED_CREDENTIALS_FILE": "/s",
    # hub settings a stdio MCP server may rely on, and process basics
    "SLACK_BOT_TOKEN": "xoxb", "GOOGLE_CLIENT_SECRET": "g", "COMPOSIO_API_KEY": "c",
    "HUBZOID_GATEWAY_ADMIN_EMAIL": "a@x", "DATABASE_SCHEMA": "s", "OTEL_EXPORTER_OTLP_HEADERS": "h",
    "PATH": "/bin", "HOME": "/home/x",
}


@pytest.fixture(autouse=True)
def _clean():
    with clean_process_env():
        yield


def test_scrubs_exactly_the_documented_keys():
    env = {**{k: "value" for k in SCRUBBED}, **KEPT}
    assert cs.child_env_overrides(env) == {k: "" for k in SCRUBBED}


def test_only_keys_present_are_touched():
    assert cs.child_env_overrides({"PATH": "/bin"}) == {}


def test_bedrock_keeps_the_aws_credentials_the_cli_signs_in_with():
    env = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_ACCESS_KEY_ID": "id", "AWS_SECRET_ACCESS_KEY": "s",
           "AWS_SESSION_TOKEN": "t", "AWS_BEARER_TOKEN_BEDROCK": "b", "AWS_SECRET_NAME": "n",
           "WEBUI_SECRET_KEY": "w"}
    assert cs.child_env_overrides(env) == {"AWS_SECRET_NAME": "", "WEBUI_SECRET_KEY": ""}


def _hub(tmp_path: Path) -> Path:
    hub = tmp_path / "hub"
    shutil.copytree(MINIMAL, hub, ignore=shutil.ignore_patterns("output", "__pycache__"))
    (hub / "restricted").mkdir()
    (hub / ".env").write_text("MODEL=claude-local\nBRIDGE_API_KEYS=bridge-key-long-enough\n"
                              "SHARED=hub-value\nSLACK_BOT_TOKEN=xoxb-hub\nCLAUDE_CODE_OAUTH_TOKEN=oauth\n")
    (hub / "restricted" / ".env").write_text(
        "TOOL_TOKEN=tool-secret\nSHARED=restricted-value\nANTHROPIC_API_KEY=sk-ant-restricted\n"
        "HUBZOID_RESTRICTED_SECRET_NAME=rsec\n")
    return hub


def test_restricted_layer_keys_are_blanked_or_reset_to_the_hub_value(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    install(monkeypatch, {"rsec": {"RESTRICTED_DB_PASSWORD": "db-secret"}})
    settings.load(hub)
    assert os.environ["TOOL_TOKEN"] == "tool-secret"  # the bridge process keeps them
    overrides = cs.child_env_overrides(os.environ)
    assert overrides["TOOL_TOKEN"] == ""
    assert overrides["RESTRICTED_DB_PASSWORD"] == ""
    assert overrides["HUBZOID_RESTRICTED_SECRET_NAME"] == ""
    assert overrides["SHARED"] == "hub-value"          # the hub layer's value, not blank
    assert overrides["BRIDGE_API_KEYS"] == ""
    for kept in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "SLACK_BOT_TOKEN", "MODEL", "PATH"):
        assert kept not in overrides


def test_claude_runtime_passes_the_overrides_to_every_child(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    (hub / "connectors" / ".mcp.json").write_text(json.dumps({"mcpServers": {"db": {
        "command": "db-mcp", "env": {"TOKEN": "${TOOL_TOKEN}", "SLACK": "${SLACK_BOT_TOKEN}"}}}}))
    install(monkeypatch, {"rsec": {"RESTRICTED_DB_PASSWORD": "db-secret"}})
    os.environ["AWS_ACCESS_KEY_ID"] = "AKIDEXAMPLE"
    from hubzoid.factory_claude import build_claude_runtime

    runtime = build_claude_runtime(hub)
    child = runtime._options.env
    assert child["TOOL_TOKEN"] == "" and child["RESTRICTED_DB_PASSWORD"] == ""
    assert child["AWS_ACCESS_KEY_ID"] == "" and child["BRIDGE_API_KEYS"] == ""
    assert child["SHARED"] == "hub-value"
    assert "ANTHROPIC_API_KEY" not in child and "SLACK_BOT_TOKEN" not in child
    # An MCP server that names a value explicitly in .mcp.json still gets it:
    # interpolation happens in the bridge, and a server's own env wins.
    assert runtime._options.mcp_servers["db"]["env"] == {"TOKEN": "tool-secret", "SLACK": "xoxb-hub"}

    # Per-turn options (here with telemetry) keep the blanks over os.environ.
    runtime._otel_endpoint = "http://127.0.0.1:4318"
    turn = runtime._options_for_turn().env
    assert turn["TOOL_TOKEN"] == "" and turn["AWS_ACCESS_KEY_ID"] == ""
    assert turn["ANTHROPIC_API_KEY"] == "sk-ant-restricted" and turn["SLACK_BOT_TOKEN"] == "xoxb-hub"
    assert turn["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318"


def test_claude_complete_passes_the_overrides(monkeypatch):
    import asyncio

    import claude_agent_sdk

    from hubzoid import factory_claude

    os.environ.update({"WEBUI_SECRET_KEY": "w", "ANTHROPIC_API_KEY": "sk-ant"})
    seen = {}

    async def fake_query(prompt, options):
        seen["env"] = options.env
        if False:
            yield None

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    asyncio.run(factory_claude.claude_complete("hi"))
    assert seen["env"] == {"WEBUI_SECRET_KEY": ""}
