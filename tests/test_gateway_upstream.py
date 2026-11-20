"""A hub whose bridge runs as its own service (HUBZOID_BRIDGE_UPSTREAM in the
hub's .env, `hubzoid run <hub> --behind <url>` on the other side): the edge
routes to it, the supervisor only checks its health, and the hub service gets
what a bridge started by the gateway would get."""
from __future__ import annotations

from pathlib import Path

import pytest

from hubzoid import cli, gateway
from hubzoid import supervisor as sup_lib
from hubzoid.settings import Settings


def _settings(hub_dir: Path, bridge_port: int, label: str) -> Settings:
    return Settings(hub_dir=Path(hub_dir).resolve(), model="claude-local", model_label=label,
                    bridge_api_keys=("k",), ui_port=3080, bridge_port=bridge_port, log_level="info",
                    max_upload_bytes=1)


def _two_hubs(tmp_path, upstream_env: str):
    local, remote = tmp_path / "sales", tmp_path / "support"
    local.mkdir(), remote.mkdir()
    (remote / ".env").write_text(upstream_env)
    load = {str(local.resolve()): _settings(local, 8000, "sales-agent"),
            str(remote.resolve()): _settings(remote, 8000, "support-agent")}
    return local, remote, (lambda h: load[str(Path(h).resolve())])


def test_an_upstream_hub_takes_no_port_and_its_routes_point_at_it(tmp_path):
    local, remote, load = _two_hubs(tmp_path, "HUBZOID_BRIDGE_UPSTREAM=http://support.hubzoid.local:8000/\n")
    gp = gateway.plan([local, remote], load=load, is_free=lambda p: True)
    by = {b.slug: b for b in gp.backends}
    # Both default to 8000: the external one takes no local port, so no clash.
    assert by["sales"].bridge_port == 8000 and not by["sales"].external
    assert by["support"].external and by["support"].bridge_url == "http://support.hubzoid.local:8000"
    routes = gp.edge_routes()
    support = {r["upstream"] for r in routes if r["hub"] == "support"}
    sales = {r["upstream"] for r in routes if r["hub"] == "sales"}
    assert support == {"http://support.hubzoid.local:8000"}
    assert sales == {"http://127.0.0.1:8000"}
    assert any(r["prefix"] == "/b/support/api" for r in routes)


@pytest.mark.parametrize("bad", ["support:8000", "ftp://h:1", "http://h:1/path", "http://u:p@h:1",
                                 "http://h:1?x=1", "http://h:notaport"])
def test_a_bad_upstream_address_stops_the_plan(tmp_path, bad):
    local, remote, load = _two_hubs(tmp_path, f"HUBZOID_BRIDGE_UPSTREAM={bad}\n")
    with pytest.raises(ValueError, match="HUBZOID_BRIDGE_UPSTREAM"):
        gateway.plan([local, remote], load=load, is_free=lambda p: True)


def test_an_inbound_hub_run_elsewhere_needs_its_inbound_address(tmp_path):
    env = ("HUBZOID_BRIDGE_UPSTREAM=http://support:8000\n"
           "TELEGRAM_BOT_TOKEN=bt\nTELEGRAM_WEBHOOK_SECRET=ws\n")
    local, remote, load = _two_hubs(tmp_path, env)
    with pytest.raises(ValueError, match="HUBZOID_INBOUND_UPSTREAM"):
        gateway.plan([local, remote], load=load, is_free=lambda p: True)
    (remote / ".env").write_text(env + "HUBZOID_INBOUND_UPSTREAM=http://support:8100\n")
    gp = gateway.plan([local, remote], load=load, is_free=lambda p: True)
    hooks = [r for r in gp.edge_routes() if r["prefix"] == "/webhooks/support"]
    assert hooks and hooks[0]["upstream"] == "http://support:8100"


# -- the supervisor ---------------------------------------------------------------------------

def _external(tmp_path, probe):
    unit = sup_lib.Unit(name="support", slug="support", specs=[], kind="external",
                        health_url="http://support:8000/healthz", bridge_url="http://support:8000")
    sup = sup_lib.Supervisor([unit], state_dir=tmp_path / "state", probe=probe, say=lambda t: None,
                             table={"default": "http://127.0.0.1:1", "routes": []}, ready_wait=1)
    return sup, unit


def _states(tmp_path) -> dict:
    import json

    return json.loads((tmp_path / "state" / "routes.json").read_text())["states"]


def test_an_external_hub_is_checked_not_started_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(sup_lib.time, "sleep", lambda s: None)
    answers = {"ok": True}
    sup, unit = _external(tmp_path, lambda u: answers["ok"])
    assert sup.start_all([unit]) == {"support": True}
    assert unit.procs == [] and _states(tmp_path) == {"support": "ready"}

    # Down: two failed checks in a row, then the edge answers "restarting".
    answers["ok"] = False
    sup._check_external(unit)
    assert unit.state == "ready"
    sup._check_external(unit)
    assert unit.state == "restarting" and _states(tmp_path) == {"support": "restarting"}

    # Never marked failed, however long it stays down.
    for _ in range(10):
        sup._retry(unit)
    assert unit.state == "restarting"

    answers["ok"] = True
    sup._retry(unit)
    assert unit.state == "ready" and _states(tmp_path) == {"support": "ready"}


def test_lifecycle_commands_say_its_own_service_runs_it(tmp_path):
    sup, unit = _external(tmp_path, lambda u: True)
    for op in ("restart", "stop", "start"):
        reply = sup.handle({"op": op, "hubs": ["support"]})
        assert reply["ok"] is False
        assert "its own service" in reply["results"][0]["error"]


# -- the hub service ---------------------------------------------------------------------------

def _hub(tmp_path, env: str = "") -> Path:
    hub = tmp_path / "support"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: support\n---\nHelp.\n")
    (hub / ".env").write_text(env)
    return hub


_GOOD = {"HUBZOID_AUTH": "true", "HUBZOID_BRIDGE_HOST": "0.0.0.0", "HUBZOID_TRUSTED_PROXIES": "10.0.0.0/16",
         "DATABASE_URL": "postgresql://db/hub", "HUBZOID_SECRET_KEY": "k"}


def test_behind_tells_the_bridge_what_the_gateway_would(tmp_path, monkeypatch):
    hub = _hub(tmp_path, "MCP_SERVER=true\n")
    for k, v in _GOOD.items():
        monkeypatch.setenv(k, v)
    env, problems = cli._behind_plan(hub, "https://hub.example.com/", dict(_GOOD))
    assert problems == []
    assert env["HUBZOID_PUBLIC_URL"] == "https://hub.example.com/b/support"
    assert env["MCP_PUBLIC_URL"] == "https://hub.example.com/b/support/mcp"
    assert env["HUBZOID_GATEWAY"] == "1" and env["HUBZOID_AUTH"] == "true"
    assert env["HUBZOID_HOST"] == env["HUBZOID_INBOUND_HOST"] == "0.0.0.0"
    assert env["HUBZOID_OPERATIONAL_DB"] == "postgresql://db/hub"
    assert cli._forwarded_args("0.0.0.0", _GOOD) == ["--forwarded-allow-ips", "10.0.0.0/16"]
    assert cli._forwarded_args("127.0.0.1", _GOOD) == []


@pytest.mark.parametrize("drop, says", [
    ("HUBZOID_TRUSTED_PROXIES", "HUBZOID_TRUSTED_PROXIES"),
    ("DATABASE_URL", "DATABASE_URL"),
    ("HUBZOID_SECRET_KEY", "HUBZOID_SECRET_KEY"),
])
def test_behind_refuses_a_setup_that_would_break(tmp_path, monkeypatch, drop, says):
    hub = _hub(tmp_path)
    env = {k: v for k, v in _GOOD.items() if k != drop}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv(drop, raising=False)
    _, problems = cli._behind_plan(hub, "https://hub.example.com", env)
    assert any(says in p for p in problems)


def test_behind_on_the_network_with_sign_in_off_is_refused(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    env = {**_GOOD, "HUBZOID_AUTH": "false"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _, problems = cli._behind_plan(hub, "https://hub.example.com", env)
    assert any("Sign-in is off" in p for p in problems)
    _, problems = cli._behind_plan(hub, "not a url", env)
    assert any("--behind" in p for p in problems)
