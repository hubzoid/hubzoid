"""A bridge registered in a gateway's deployment keeps the mode and sign-in the
gateway recorded in its manifest, whatever its own environment or hub .env says.

`hubzoid gateway --no-bridges` cannot pin HUBZOID_AUTH into bridges that run as
their own units. Such a bridge read `WEBUI_AUTH=false` from its hub .env before
the manifest's `auth: true`, so it ran in local mode behind the public edge and
served every request addressed to an IP address (or the public origin) as the
local owner, an administrator. Now the manifest wins, and a bridge whose
settings disagree with it refuses to start. A standalone hub and a legacy
(Open WebUI) deployment's sign-in behave as before.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request
from typer.testing import CliRunner

import hubzoid.access as access
from hubzoid import appmode, cli, deployment, secretbox
from hubzoid import settings as settingslib
from hubzoid.auth import current_user, sessions
from tests._fake_secrets import clean_process_env


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(cli, "_wait_for", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_ensure_port_available", lambda *a: None)
    monkeypatch.setattr(cli, "_wait_any", lambda procs, **k: None)
    monkeypatch.setattr(cli, "_stop_groups", lambda procs, **k: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *a, **k: None)
    with clean_process_env():
        access._stores.clear()
        secretbox.reset_cache()
        sessions.reset_cache()
        yield
        access._stores.clear()
        secretbox.reset_cache()
        sessions.reset_cache()


@pytest.fixture
def started(monkeypatch):
    calls: list[list[str]] = []

    def fake_popen(cmd, *a, env=None, **kw):
        calls.append([str(c) for c in cmd])
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    return calls


def _hub(root: Path, name: str, port: int, env: str = "") -> Path:
    hub = root / name
    hub.mkdir(parents=True)
    (hub / "AGENTS.md").write_text(f"---\nname: {name}\ndescription: The {name} agent\n---\nbody")
    (hub / ".env").write_text(f"BRIDGE_PORT={port}\nMODEL=claude-local\n"
                              f"BRIDGE_API_KEYS=a-bridge-key-long-enough\n{env}")
    return hub


def _gateway(tmp_path: Path, started, *, sales_env: str = "WEBUI_AUTH=false\n"):
    """`hubzoid gateway sales support --no-bridges` with sign-in on, as in the
    review: the first hub's .env still says WEBUI_AUTH=false (a 1.0.x leftover)."""
    sales = _hub(tmp_path, "sales", 4161, sales_env)
    support = _hub(tmp_path, "support", 4162)
    gw = tmp_path / "gw"
    with clean_process_env(HUBZOID_AUTH="true"):
        result = CliRunner().invoke(cli.app, ["gateway", str(sales), str(support), "--port", "4160",
                                              "--data-dir", str(gw), "--no-bridges"],
                                    catch_exceptions=False)
    assert result.exit_code == 0, result.output
    manifest = json.loads((gw / "deployment.json").read_text())
    assert manifest["auth"] is True and manifest["ui_mode"] == "hubzoid"
    started.clear()
    return sales, support, gw


def _request(host: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/auth/session", "query_string": b"",
                    "headers": [(b"host", host.encode())], "server": ("127.0.0.1", 4161)})


def test_a_separately_started_bridge_keeps_the_deployments_sign_in(tmp_path, started):
    sales, _support, _gw = _gateway(tmp_path, started)
    # The bridge's own unit: none of the gateway's environment. It loads every
    # configuration layer itself, so the hub .env's WEBUI_AUTH=false arrives.
    with clean_process_env():
        settingslib.load(sales)
        assert os.environ["WEBUI_AUTH"] == "false"
        assert appmode.auth_enabled(sales) is True
        assert appmode.mode_summary(sales)["auth"] is True
        # No session: nobody, never the local owner (an administrator).
        assert current_user(_request("203.0.113.7"), sales) is None


def test_a_bridge_that_contradicts_its_deployment_refuses_to_start(tmp_path, started):
    sales, _support, _gw = _gateway(tmp_path, started)
    with clean_process_env():
        result = CliRunner().invoke(cli.app, ["run", str(sales), "--no-ui", "--bridge-port", "4161"])
    assert result.exit_code == 2, result.output
    assert "WEBUI_AUTH=false" in result.output and "sign-in on" in result.output
    assert started == []
    # A bridge started without the CLI stops before serving anything as well.
    from hubzoid import server

    with clean_process_env(HUBZOID_HUB_DIR=str(sales)):
        with pytest.raises(RuntimeError, match="WEBUI_AUTH=false"):
            server.build_app()


def test_a_bridge_that_agrees_with_its_deployment_starts(tmp_path, started):
    sales, _support, _gw = _gateway(tmp_path, started)
    # HUBZOID_AUTH wins over WEBUI_AUTH, as it does for the bridges the gateway
    # launches (it pins HUBZOID_AUTH for them).
    with clean_process_env(HUBZOID_AUTH="true"):
        result = CliRunner().invoke(cli.app, ["run", str(sales), "--no-ui", "--bridge-port", "4161"])
        assert appmode.deployment_conflicts(sales) == []
    assert result.exit_code == 0, result.output
    assert len(started) == 1 and "hubzoid.server:build_app" in started[0]


def _manifest(tmp_path: Path, hub: Path, **recorded) -> Path:
    path = tmp_path / "gw" / "deployment.json"
    deployment.save(path, hubs=[dict(key=hub.name, name=hub.name, path=str(hub), model_id=hub.name,
                                     slug=hub.name)],
                    operational_url=f"sqlite:///{tmp_path / 'op.db'}", owui_url="", owui_db="", **recorded)
    return path


@pytest.mark.parametrize("recorded,asked,legacy", [
    ("hubzoid", "openwebui", False),
    ("openwebui", "hubzoid", True),
])
def test_the_deployments_mode_wins_too(tmp_path, recorded, asked, legacy):
    hub = _hub(tmp_path, "sales", 4163)
    _manifest(tmp_path, hub, ui_mode=recorded, auth=True)
    with clean_process_env(HUBZOID_UI=asked):
        assert appmode.is_openwebui(hub) is legacy
        problems = appmode.deployment_conflicts(hub)
    assert len(problems) == 1 and f"HUBZOID_UI={asked}" in problems[0]


def test_a_running_bridge_follows_the_manifest(tmp_path):
    hub = _hub(tmp_path, "sales", 4164)
    path = _manifest(tmp_path, hub, ui_mode="hubzoid", auth=False)
    with clean_process_env(WEBUI_AUTH="false"):
        assert appmode.auth_enabled(hub) is False and appmode.deployment_conflicts(hub) == []
        _manifest(tmp_path, hub, ui_mode="hubzoid", auth=True)   # the gateway turned sign-in on
        assert appmode.auth_enabled(hub) is True
        assert "WEBUI_AUTH=false" in " ".join(appmode.deployment_conflicts(hub))
    assert path.is_file()


def test_standalone_and_legacy_sign_in_are_unchanged(tmp_path):
    hub = _hub(tmp_path, "solo", 4165)
    with clean_process_env(WEBUI_AUTH="false"):
        assert appmode.auth_enabled(hub) is False and appmode.deployment_conflicts(hub) == []
    with clean_process_env(HUBZOID_AUTH="true", WEBUI_AUTH="false"):
        assert appmode.auth_enabled(hub) is True and appmode.deployment_conflicts(hub) == []
    with clean_process_env(HUBZOID_UI="openwebui"):
        assert appmode.is_openwebui(hub) and appmode.deployment_conflicts(hub) == []
    # A legacy deployment: Open WebUI owns sign-in; the environment decides first, as in 1.1.
    _manifest(tmp_path, hub, ui_mode="openwebui", auth=True)
    with clean_process_env(WEBUI_AUTH="false"):
        assert appmode.auth_enabled(hub) is False and appmode.deployment_conflicts(hub) == []
    # A 1.0.x manifest records neither: the environment decides, as before.
    _manifest(tmp_path, hub)
    with clean_process_env(WEBUI_AUTH="false", HUBZOID_UI="openwebui"):
        assert appmode.auth_enabled(hub) is False and appmode.is_openwebui(hub)
        assert appmode.deployment_conflicts(hub) == []
