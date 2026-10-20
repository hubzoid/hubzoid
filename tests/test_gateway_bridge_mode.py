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


def _gateway(tmp_path: Path, started, *, sales_env: str = ""):
    """`hubzoid gateway sales support --no-bridges` with sign-in on."""
    sales = _hub(tmp_path, "sales", 4161, sales_env)
    support = _hub(tmp_path, "support", 4162)
    gw = tmp_path / "gw"
    with clean_process_env(HUBZOID_AUTH="true"):
        result = CliRunner().invoke(cli.app, ["gateway", str(sales), str(support), "--port", "4160",
                                              "--data-dir", str(gw)],
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
    # Later, someone turns sign-in off in the hub's own .env. The bridge's own
    # unit (none of the gateway's environment) keeps the recorded sign-in and
    # refuses to start rather than serve everyone as the local owner.
    (sales / ".env").write_text((sales / ".env").read_text() + "HUBZOID_AUTH=false\n")
    with clean_process_env():
        settingslib.load(sales)
        assert appmode.auth_enabled(sales) is True
        assert appmode.mode_summary(sales)["auth"] is True
        assert current_user(_request("203.0.113.7"), sales) is None
        result = CliRunner().invoke(cli.app, ["run", str(sales), "--no-ui", "--bridge-port", "4161"])
    assert result.exit_code == 2, result.output
    assert "HUBZOID_AUTH=false" in result.output and "sign-in on" in result.output
    assert started == []
    from hubzoid import server

    with clean_process_env(HUBZOID_HUB_DIR=str(sales)):
        with pytest.raises(RuntimeError, match="HUBZOID_AUTH=false"):
            server.build_app()


def test_an_open_webui_sign_in_name_stops_a_separately_started_bridge(tmp_path, started):
    sales, _support, _gw = _gateway(tmp_path, started)
    (sales / ".env").write_text((sales / ".env").read_text() + "WEBUI_AUTH=true\n")
    with clean_process_env():
        result = CliRunner().invoke(cli.app, ["run", str(sales), "--no-ui", "--bridge-port", "4161"])
    assert result.exit_code == 2 and "WEBUI_AUTH" in result.output and "HUBZOID_AUTH" in result.output
    assert started == []


def test_a_bridge_that_agrees_with_its_deployment_starts(tmp_path, started):
    sales, _support, _gw = _gateway(tmp_path, started)
    with clean_process_env(HUBZOID_AUTH="true"):
        result = CliRunner().invoke(cli.app, ["run", str(sales), "--no-ui", "--bridge-port", "4161"])
        assert appmode.deployment_conflicts(sales) == []
    assert result.exit_code == 0, result.output
    assert len(started) == 1 and "hubzoid.server:build_app" in started[0]


def _manifest(tmp_path: Path, hub: Path, **recorded) -> Path:
    path = tmp_path / "gw" / "deployment.json"
    deployment.save(path, hubs=[dict(key=hub.name, name=hub.name, path=str(hub), model_id=hub.name,
                                     slug=hub.name)],
                    operational_url=f"sqlite:///{tmp_path / 'op.db'}", owui_db="", **recorded)
    return path


def test_an_open_webui_deployment_or_mode_stops_the_bridge(tmp_path):
    hub = _hub(tmp_path, "sales", 4163)
    _manifest(tmp_path, hub, ui_mode="openwebui", auth=True)
    with clean_process_env():
        assert " ".join(appmode.deployment_conflicts(hub)).count("Open WebUI mode") == 1
    _manifest(tmp_path, hub, ui_mode="hubzoid", auth=True)
    with clean_process_env(HUBZOID_UI="openwebui"):
        problems = appmode.deployment_conflicts(hub)
    assert len(problems) == 1 and "HUBZOID_UI=openwebui" in problems[0]


def test_a_running_bridge_follows_the_manifest(tmp_path):
    hub = _hub(tmp_path, "sales", 4164)
    path = _manifest(tmp_path, hub, ui_mode="hubzoid", auth=False)
    with clean_process_env(HUBZOID_AUTH="false"):
        assert appmode.auth_enabled(hub) is False and appmode.deployment_conflicts(hub) == []
        _manifest(tmp_path, hub, ui_mode="hubzoid", auth=True)   # the gateway turned sign-in on
        assert appmode.auth_enabled(hub) is True
        assert "HUBZOID_AUTH=false" in " ".join(appmode.deployment_conflicts(hub))
    assert path.is_file()


def test_a_separately_started_bridge_trusts_every_address_of_its_deployment(tmp_path, started):
    """One site at two addresses: the gateway's --public-url is one, and
    some hubs' .env give the other. The first hub's bridge serves every agent's
    sign-in pages, so a sign-in started on either address returns to it."""
    from hubzoid.connectors import oauth_flow

    sales = _hub(tmp_path, "sales", 4166, "HUBZOID_PUBLIC_URL=https://one.example.org/b/sales\n")
    support = _hub(tmp_path, "support", 4167, "HUBZOID_PUBLIC_URL=https://two.example.org/b/support\n")
    gw = tmp_path / "gw"
    with clean_process_env(HUBZOID_AUTH="true"):
        result = CliRunner().invoke(cli.app, ["gateway", str(sales), str(support), "--port", "4160",
                                              "--data-dir", str(gw),
                                              "--public-url", "https://one.example.org"],
                                    catch_exceptions=False)
    assert result.exit_code == 0, result.output
    both = ["https://one.example.org", "https://two.example.org"]
    assert json.loads((gw / "deployment.json").read_text())["allowed_origins"] == both

    def asked_on(host):
        return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                        "headers": [(b"host", host.encode()), (b"x-forwarded-proto", b"https")]})

    with clean_process_env(HUBZOID_HUB_DIR=str(sales),
                           HUBZOID_PUBLIC_URL="https://one.example.org/b/sales"):
        assert appmode.allowed_origins() == both
        assert oauth_flow.origin_for(asked_on("two.example.org")) == "https://two.example.org"
        # Never an address only the request names.
        assert oauth_flow.origin_for(asked_on("evil.example.net")) == "https://one.example.org"
        # Settings passed in explicitly are judged alone.
        assert appmode.allowed_origins({"HUBZOID_PUBLIC_URL": "https://one.example.org"}) == both[:1]


def test_standalone_sign_in(tmp_path):
    hub = _hub(tmp_path, "solo", 4165)
    with clean_process_env(HUBZOID_AUTH="false"):
        assert appmode.auth_enabled(hub) is False and appmode.deployment_conflicts(hub) == []
    with clean_process_env(HUBZOID_AUTH="true"):
        assert appmode.auth_enabled(hub) is True and appmode.deployment_conflicts(hub) == []
    # Open WebUI's name never decides sign-in, and never passes unnoticed.
    with clean_process_env(WEBUI_AUTH="true"):
        assert appmode.auth_enabled(hub) is False
        assert "WEBUI_AUTH" in " ".join(appmode.deployment_conflicts(hub))
    # A 1.0.x manifest records no sign-in: the environment decides.
    _manifest(tmp_path, hub)
    with clean_process_env(HUBZOID_AUTH="false"):
        assert appmode.auth_enabled(hub) is False and appmode.deployment_conflicts(hub) == []


def test_a_separately_started_bridge_names_retired_settings_that_only_warn(tmp_path, started,
                                                                            monkeypatch, caplog):
    from hubzoid import migrations, server

    sales, _support, _gw = _gateway(tmp_path, started)
    (sales / ".env").write_text((sales / ".env").read_text() + "HUBZOID_OWUI_PORT=3001\n")

    class Stop(Exception):
        pass

    def stop(*a, **k):  # the warnings come before the schema step; stop there
        raise Stop

    monkeypatch.setattr(migrations, "upgrade", stop)
    with clean_process_env(HUBZOID_HUB_DIR=str(sales), HUBZOID_AUTH="true"), \
            caplog.at_level("WARNING", logger="hubzoid.server"):
        with pytest.raises(Stop):
            server.build_app()
    assert "HUBZOID_OWUI_PORT is an Open WebUI setting" in caplog.text
