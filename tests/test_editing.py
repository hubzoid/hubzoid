"""Changing agent files from the browser: Beta, off by default (hubzoid.editing,
HUB-140 D9 and D10). The browser and the agent's propose tools are refused,
the CLI is not, and files that come from an image are never written."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hubzoid import editing

MINIMAL = Path(__file__).parent / "fixtures" / "minimal_hub"


def test_where_the_files_come_from_decides_the_message(tmp_path, monkeypatch):
    monkeypatch.delenv(editing.ENV, raising=False)
    monkeypatch.delenv("HUBZOID_DATA_DIR", raising=False)
    hub = tmp_path / "hub"
    hub.mkdir()
    assert editing.source(hub) == "local" and not editing.allowed(hub)
    assert "on this computer" in editing.message(hub)
    (tmp_path / ".git").mkdir()
    assert editing.source(hub) == "git" and "your repository" in editing.message(hub)
    monkeypatch.setenv(editing.ENV, "true")
    assert editing.allowed(hub) and editing.refusal(hub) is None
    monkeypatch.setenv("HUBZOID_DATA_DIR", str(tmp_path / "data"))     # files from an image
    assert editing.source(hub) == "image" and not editing.allowed(hub)


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    hub = tmp_path / "hub"
    shutil.copytree(MINIMAL, hub, ignore=shutil.ignore_patterns(".hubzoid"))
    for var in (editing.ENV, "HUBZOID_AUTH", "HUBZOID_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "hubzoid-test/scripted")
    monkeypatch.setenv("HUBZOID_TEST_RUNTIME", "1")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    from hubzoid.server import build_app

    # Sign-in off: a request to this machine is the local owner.
    return TestClient(build_app(), base_url="http://127.0.0.1"), hub


def test_the_browser_is_refused_while_the_switch_is_off(bridge, monkeypatch):
    client, hub = bridge
    status = client.get("/api/editing").json()
    assert status["editable"] is False and status["files_from"] == "local" and status["message"]
    r = client.post("/api/hub-changes", json={"title": "x", "edits": [
        {"path": "AGENTS.md", "content": "new"}]}, headers={"origin": "http://127.0.0.1"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "in_app_editing_off"
    r = client.put("/api/hitl/config", json={"text": "version: 1\n", "expected_revision": "absent"},
                   headers={"origin": "http://127.0.0.1"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "in_app_editing_off"
    monkeypatch.setenv(editing.ENV, "true")
    assert client.get("/api/editing").json()["editable"] is True
    r = client.put("/api/hitl/config", json={"text": "version: 1\n", "expected_revision": "absent"},
                   headers={"origin": "http://127.0.0.1"})
    assert r.status_code != 409 or r.json()["detail"]["code"] != "in_app_editing_off"


def test_the_agent_does_not_offer_shared_changes_while_off(tmp_path, monkeypatch):
    import asyncio
    import json

    from agents import function_tool

    from hubzoid.hub_changes import tools as hc_tools

    monkeypatch.delenv(editing.ENV, raising=False)
    monkeypatch.delenv("HUBZOID_DATA_DIR", raising=False)

    class Access:
        @staticmethod
        def guard_tool(ft, permission, hub_dir):
            from hubzoid.tool_classes import copy_tool

            return copy_tool(ft, is_enabled=lambda *a, **k: True)

    @function_tool(name_override="propose_hub_change")
    def propose() -> str:
        """Propose."""
        return "proposed"

    @function_tool(name_override="read_hub_file")
    def read() -> str:
        """Read."""
        return "read"

    hub = tmp_path / "hub"
    hub.mkdir()
    writes, reads = hc_tools.gate(propose, hub, Access, "curator"), hc_tools.gate(read, hub, Access, "curator")
    assert writes.is_enabled() is False and reads.is_enabled() is not False
    out = json.loads(asyncio.run(writes.on_invoke_tool(None, "{}")))
    assert "from the browser is off" in out["error"]
    monkeypatch.setenv(editing.ENV, "true")
    assert writes.is_enabled() is not False
