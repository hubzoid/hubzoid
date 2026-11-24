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



# -- the MCP address -------------------------------------------------------------------------

_MCP_UP = "HUBZOID_BRIDGE_UPSTREAM=http://support:8000\nMCP_SERVER=true\n"


def _mcp_url(gp, slug="support"):
    return {b.slug: b for b in gp.backends}[slug].mcp_public_url


def test_an_mcp_hub_without_its_own_address_gets_the_edges(tmp_path):
    local, remote, load = _two_hubs(tmp_path, _MCP_UP)
    gp = gateway.plan([local, remote], load=load, is_free=lambda p: True,
                      public_base="https://hub.example.com/")
    # The same address `run support --behind https://hub.example.com` gives it.
    assert _mcp_url(gp) == "https://hub.example.com/b/support/mcp"
    gp = gateway.plan([local, remote], load=load, is_free=lambda p: True,
                      public_base="http://127.0.0.1:3080")
    assert _mcp_url(gp) == "http://127.0.0.1:3080/b/support/mcp"


def test_an_mcp_hubs_own_address_wins(tmp_path):
    local, remote, load = _two_hubs(tmp_path, _MCP_UP + "MCP_PUBLIC_URL=https://other.example.com/b/support/mcp\n")
    gp = gateway.plan([local, remote], load=load, is_free=lambda p: True,
                      public_base="https://hub.example.com")
    assert _mcp_url(gp) == "https://other.example.com/b/support/mcp"


def test_an_mcp_hub_with_no_address_to_build_from_says_what_to_set(tmp_path):
    local, remote, load = _two_hubs(tmp_path, _MCP_UP)
    with pytest.raises(ValueError) as caught:
        gateway.plan([local, remote], load=load, is_free=lambda p: True)
    said = str(caught.value)
    assert "support" in said and "public_url" in said and "MCP_PUBLIC_URL=" in said
    assert "HTTPS" not in said


def test_an_mcp_hub_on_a_plain_http_public_address_names_the_hub(tmp_path):
    local, remote, load = _two_hubs(tmp_path, _MCP_UP)
    with pytest.raises(ValueError, match=r"support .*http://hub\.example\.com/b/support/mcp.*HTTPS"):
        gateway.plan([local, remote], load=load, is_free=lambda p: True,
                     public_base="http://hub.example.com")


def test_an_empty_mcp_address_line_in_the_hub_env_stops_the_plan(tmp_path):
    # The hub's bridge loads its .env over the environment, so a planned
    # address could never reach it past an empty line.
    local, remote, load = _two_hubs(tmp_path, _MCP_UP + "MCP_PUBLIC_URL=\n")
    with pytest.raises(ValueError, match=r"MCP_PUBLIC_URL is empty in support/\.env.*https://hub\.example\.com/b/support/mcp"):
        gateway.plan([local, remote], load=load, is_free=lambda p: True,
                     public_base="https://hub.example.com")

def test_an_empty_mcp_address_says_it_is_not_set():
    from hubzoid.mcp_oauth import validate_public_url

    for empty in ("", "  ", None):
        with pytest.raises(ValueError, match="MCP_PUBLIC_URL is not set"):
            validate_public_url(empty)


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


def _good(tmp_path, hub) -> dict:
    """A hub service's environment that works: the edge's manifest on a
    shared folder, a random bridge key, the deployment key and database."""
    import json as _json

    manifest = tmp_path / "edge-data" / "deployment.json"
    manifest.parent.mkdir()
    manifest.write_text(_json.dumps({"version": 1, "hubs": [
        {"key": "support", "path": str(hub), "slug": "support", "name": "Support"}]}))
    return {"HUBZOID_AUTH": "true", "HUBZOID_BRIDGE_HOST": "0.0.0.0", "HUBZOID_TRUSTED_PROXIES": "10.0.0.0/16",
            "DATABASE_URL": "postgresql://db/hub", "HUBZOID_SECRET_KEY": "k",
            "HUBZOID_DEPLOYMENT": str(manifest), "BRIDGE_API_KEYS": "hzb_random-and-long"}


def _set(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_behind_tells_the_bridge_what_the_gateway_would(tmp_path, monkeypatch):
    hub = _hub(tmp_path, "MCP_SERVER=true\n")
    good = _good(tmp_path, hub)
    _set(monkeypatch, good)
    env, problems = cli._behind_plan(hub, "https://hub.example.com/", good)
    assert problems == []
    assert env["HUBZOID_PUBLIC_URL"] == "https://hub.example.com/b/support"
    assert env["MCP_PUBLIC_URL"] == "https://hub.example.com/b/support/mcp"
    assert env["HUBZOID_GATEWAY"] == "1" and env["HUBZOID_AUTH"] == "true"
    assert env["HUBZOID_HOST"] == env["HUBZOID_INBOUND_HOST"] == "0.0.0.0"
    assert env["HUBZOID_OPERATIONAL_DB"] == "postgresql://db/hub"
    assert cli._forwarded_args("0.0.0.0", good) == ["--forwarded-allow-ips", "10.0.0.0/16"]
    assert cli._forwarded_args("127.0.0.1", good) == []


@pytest.mark.parametrize("change, says", [
    ({"HUBZOID_TRUSTED_PROXIES": None}, "HUBZOID_TRUSTED_PROXIES"),
    ({"DATABASE_URL": None}, "DATABASE_URL"),
    ({"HUBZOID_SECRET_KEY": None}, "HUBZOID_SECRET_KEY"),
    ({"HUBZOID_DEPLOYMENT": None}, "manifest"),
    ({"BRIDGE_API_KEYS": "dev"}, "public 'dev'"),
    ({"HUBZOID_AUTH": "false"}, "Sign-in is off"),
])
def test_behind_refuses_a_setup_that_would_break(tmp_path, monkeypatch, change, says):
    hub = _hub(tmp_path)
    env = _good(tmp_path, hub)
    for k, v in change.items():
        if v is None:
            env.pop(k)
            monkeypatch.delenv(k, raising=False)
        else:
            env[k] = v
    _set(monkeypatch, env)
    _, problems = cli._behind_plan(hub, "https://hub.example.com", env)
    assert any(says in p for p in problems), problems
    _, problems = cli._behind_plan(hub, "not a url", env)
    assert any("--behind" in p for p in problems)


def test_database_pool_settings_for_a_server_database():
    from hubzoid import db

    args: dict = {}
    opts = db.pool_options({"HUBZOID_DB_POOL_SIZE": "3", "HUBZOID_DB_MAX_OVERFLOW": "2",
                            "HUBZOID_DB_POOL_RECYCLE": "1700", "HUBZOID_DB_POOL_TIMEOUT": "x",
                            "HUBZOID_DB_CONNECT_TIMEOUT": "5"}, args)
    assert opts == {"pool_size": 3, "max_overflow": 2, "pool_recycle": 1700}
    assert args == {"connect_timeout": 5}
    assert db.pool_options({}, {}) == {}


def test_sqlite_on_a_network_filesystem_is_found():
    from hubzoid import doctor

    mounts = ("/dev/root / ext4 rw 0 0\n"
              "fs-1.efs.ap-south-1.amazonaws.com:/ /data nfs4 rw 0 0\n")
    assert doctor.network_filesystem(Path("/data/hubs/x/.hubzoid"), mounts) == "nfs4"
    assert doctor.network_filesystem(Path("/srv/app"), mounts) is None
    assert doctor.network_filesystem(Path("/database"), mounts) is None


def test_control_from_another_host_says_where_the_deployment_runs(tmp_path):
    import json as _json

    (tmp_path / "control.json").write_text(_json.dumps({"socket": "/nope.sock", "host": "other-task"}))
    with pytest.raises(ConnectionError, match="runs on other-task"):
        sup_lib.request(tmp_path, {"op": "status"})
