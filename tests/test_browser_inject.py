"""Unit tests for the shared-browser MCP auto-injection (HUBZOID_BROWSER).

These exercise the loader's injection logic and the env->spec helper; they do
not spawn a browser or a sidecar (see test_browser_e2e.py for that).
"""
from __future__ import annotations

import json

import pytest

from hubzoid import browser as browserlib
from hubzoid.loaders import mcp as mcp_loader


@pytest.fixture(autouse=True)
def _clean_browser_env(monkeypatch):
    """Start each test from a known-empty browser env."""
    for var in (
        "HUBZOID_BROWSER",
        "HUBZOID_BROWSER_PORT",
        "HUBZOID_BROWSER_MCP_URL",
        "HUBZOID_BROWSER_CDP_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _hub(tmp_path, servers=None):
    """A hub dir; with a connectors/.mcp.json when `servers` is given."""
    if servers is not None:
        conn = tmp_path / "connectors"
        conn.mkdir()
        (conn / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    return tmp_path


def test_disabled_no_injection(tmp_path):
    out = mcp_loader.load_all_raw(_hub(tmp_path, {}))
    assert "playwright" not in out
    assert browserlib.mcp_config_entry_from_env() is None


def test_enabled_injects_streamable_http(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_BROWSER", "true")
    monkeypatch.setenv("HUBZOID_BROWSER_PORT", "8931")
    out = mcp_loader.load_all_raw(_hub(tmp_path, {}))
    assert out["playwright"]["transport"] == "streamable-http"
    assert out["playwright"]["url"] == "http://localhost:8931/mcp"


def test_enabled_respects_custom_port(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_BROWSER", "on")
    monkeypatch.setenv("HUBZOID_BROWSER_PORT", "9999")
    out = mcp_loader.load_all_raw(_hub(tmp_path, {}))
    assert out["playwright"]["url"] == "http://localhost:9999/mcp"


def test_external_mcp_url_used_verbatim(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_BROWSER", "1")
    monkeypatch.setenv("HUBZOID_BROWSER_MCP_URL", "http://playwright-mcp:8931/mcp")
    out = mcp_loader.load_all_raw(_hub(tmp_path, {}))
    assert out["playwright"]["url"] == "http://playwright-mcp:8931/mcp"
    assert out["playwright"]["transport"] == "streamable-http"


def test_sse_url_picks_sse_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_BROWSER", "yes")
    monkeypatch.setenv("HUBZOID_BROWSER_MCP_URL", "http://legacy-host/sse")
    out = mcp_loader.load_all_raw(_hub(tmp_path, {}))
    assert out["playwright"]["transport"] == "sse"


def test_manual_playwright_entry_wins(tmp_path, monkeypatch):
    """A hub that defines its own `playwright` server is not overridden."""
    monkeypatch.setenv("HUBZOID_BROWSER", "true")
    manual = {"playwright": {"transport": "sse", "url": "http://manual/sse"}}
    out = mcp_loader.load_all_raw(_hub(tmp_path, manual))
    assert out["playwright"]["url"] == "http://manual/sse"


def test_injection_without_connectors_dir(tmp_path, monkeypatch):
    """Injection works even when the hub has no connectors/ folder at all."""
    monkeypatch.setenv("HUBZOID_BROWSER", "true")
    out = mcp_loader.load_all_raw(tmp_path)  # no connectors dir created
    assert "playwright" in out


def test_injection_coexists_with_other_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_BROWSER", "true")
    servers = {"filesystem": {"command": "npx", "args": ["x"]}}
    out = mcp_loader.load_all_raw(_hub(tmp_path, servers))
    assert "filesystem" in out and "playwright" in out


def test_disabled_leaves_other_servers_untouched(tmp_path):
    servers = {"filesystem": {"command": "npx", "args": ["x"]}}
    out = mcp_loader.load_all_raw(_hub(tmp_path, servers))
    assert set(out) == {"filesystem"}


# --- Claude backend shape (load_all_claude) -------------------------------

def test_claude_shape_translates_injected_browser(tmp_path, monkeypatch):
    """The injected browser becomes a valid Claude `{type: http, url}` config."""
    monkeypatch.setenv("HUBZOID_BROWSER", "true")
    monkeypatch.setenv("HUBZOID_BROWSER_PORT", "8931")
    cl = mcp_loader.load_all_claude(_hub(tmp_path, {}))
    assert cl["playwright"] == {"type": "http", "url": "http://localhost:8931/mcp"}
    # No neutral-only / OpenAI-only keys leak to the Claude SDK.
    assert "transport" not in cl["playwright"]
    assert "client_session_timeout_seconds" not in cl["playwright"]


def test_claude_shape_sse_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_BROWSER", "true")
    monkeypatch.setenv("HUBZOID_BROWSER_MCP_URL", "http://legacy/sse")
    cl = mcp_loader.load_all_claude(_hub(tmp_path, {}))
    assert cl["playwright"] == {"type": "sse", "url": "http://legacy/sse"}


def test_claude_shape_stdio_server(tmp_path):
    servers = {"fs": {"command": "npx", "args": ["a", "b"], "env": {"K": "v"}}}
    cl = mcp_loader.load_all_claude(_hub(tmp_path, servers))
    assert cl["fs"] == {"type": "stdio", "command": "npx", "args": ["a", "b"], "env": {"K": "v"}}
