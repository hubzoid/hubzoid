"""`hubzoid run` in the default mode: the Hubzoid web app, no Open WebUI.

Processes are never started: Popen is replaced and the readiness probe answers
at once. Open WebUI mode (HUBZOID_UI=openwebui) is checked for the
extra and for keeping its 1.0.x processes.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from hubzoid import branding, cli, webui
from tests._fake_secrets import clean_process_env

ROOT = Path(__file__).resolve().parents[1]
_WAIT_ANY = cli._wait_any


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    # Never stamp branding into, or patch, an installed Open WebUI.
    monkeypatch.setattr(branding, "static_dirs", lambda: [])
    monkeypatch.setattr(webui, "_patch_owui_suffix", lambda strip: None)
    monkeypatch.setattr(webui, "_patch_owui_branding", lambda brand, strip: None)
    monkeypatch.setattr(webui, "_find_binary", lambda: "/fake/open-webui")
    monkeypatch.setattr(cli, "_wait_for", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_wait_any", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_stop_groups", cli._stop_processes)
    monkeypatch.setattr(cli, "_ensure_port_available", lambda *a: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_open_browser", lambda url: None)
    with clean_process_env():
        yield


def _fake_popen(record):
    """A stand-in for subprocess.Popen. A class, so modules imported while it is
    in place can still write `subprocess.Popen[bytes]` in annotations."""

    class FakePopen:
        def __class_getitem__(cls, item):
            return cls

        def __new__(cls, cmd, *a, env=None, **kw):
            return record(cmd, env)

    return FakePopen


@pytest.fixture
def launched(monkeypatch):
    calls: list[dict] = []

    def record(cmd, env):
        calls.append({"cmd": [str(c) for c in cmd], "env": dict(env or {})})
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        return proc

    monkeypatch.setattr(subprocess, "Popen", _fake_popen(record))
    return calls


def _hub(tmp_path: Path, env: str = "", name: str = "ops") -> Path:
    hub = tmp_path / name
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: Ops Desk\ndescription: d\n---\nbody")
    (hub / ".env").write_text("BRIDGE_API_KEYS=a-bridge-key-long-enough\n" + env)
    return hub


def _run(hub: Path, *args: str):
    return CliRunner().invoke(cli.app, ["run", str(hub), "--port", "3801", "--bridge-port", "3802",
                                        "--no-open", *args])


def _flat(text: str) -> str:
    """Output without whitespace: Rich wraps long paths mid-word at the test width."""
    return "".join(text.split())


def _kind(calls, marker):
    return [c for c in calls if marker in " ".join(c["cmd"])]


def _bridge(calls):
    (call,) = _kind(calls, "hubzoid.server:build_app")
    return call["env"]


def _edge(calls):
    (call,) = _kind(calls, "hubzoid.edge:_factory")
    return call


# ---------------------------------------------------------------------------
# Processes and routing
# ---------------------------------------------------------------------------
def test_default_mode_runs_bridge_and_edge_only(tmp_path, launched):
    result = _run(_hub(tmp_path))
    assert result.exit_code == 0, result.output
    assert not [c for c in launched if c["cmd"][0] == "/fake/open-webui"]
    assert len(launched) == 2
    edge = _edge(launched)
    assert edge["env"]["HUBZOID_EDGE_DEFAULT"] == "http://127.0.0.1:3802"  # every path to the bridge
    assert json.loads(edge["env"]["HUBZOID_EDGE_ROUTES"]) == []
    assert edge["env"]["HUBZOID_UI"] == "hubzoid"
    assert edge["cmd"][edge["cmd"].index("--host") + 1] == "127.0.0.1"
    assert edge["cmd"][edge["cmd"].index("--port") + 1] == "3801"
    bridge = _bridge(launched)
    assert "OWUI_INTERNAL_URL" not in bridge and bridge["BRIDGE_PORT"] == "3802"
    assert "Hubzoid is ready: http://127.0.0.1:3801" in " ".join(result.output.split())


def test_webhooks_go_to_the_inbound_process(tmp_path, launched):
    hub = _hub(tmp_path, "WEBHOOK_INBOUND_SECRET=s3cr3t-value-long\nWEBHOOK_INBOUND_NAME=alerts\n")
    result = _run(hub, "--webhook")
    assert result.exit_code == 0, result.output
    routes = json.loads(_edge(launched)["env"]["HUBZOID_EDGE_ROUTES"])
    assert len(routes) == 1 and routes[0]["prefix"] == "/webhooks/ops"
    assert routes[0]["upstream"].startswith("http://127.0.0.1:")
    assert _kind(launched, "inbound")  # the inbound process was started


def test_no_ui_is_the_bridge_alone(tmp_path, launched):
    result = _run(_hub(tmp_path), "--no-ui")
    assert result.exit_code == 0, result.output
    assert [c["cmd"] for c in launched] == [c["cmd"] for c in _kind(launched, "hubzoid.server:build_app")]
    assert "MCP_SERVER" not in _bridge(launched)


def test_hub_only_layers_stay_out_of_the_edge(tmp_path, launched):
    hub = _hub(tmp_path, "SHARED=hub\n")
    (hub / "restricted").mkdir()
    (hub / "restricted" / ".env").write_text("TOOL_TOKEN=restricted-token\nSHARED=restricted\n")
    result = _run(hub)
    assert result.exit_code == 0, result.output
    for env in (_bridge(launched), _edge(launched)["env"]):
        assert "TOOL_TOKEN" not in env and env["SHARED"] == "hub"


def test_an_edge_that_cannot_bind_stops_the_run(tmp_path, monkeypatch):
    stopped = []

    def record(cmd, env):
        proc = MagicMock()
        is_edge = "hubzoid.edge:_factory" in cmd
        proc.poll.return_value = 1 if is_edge else None
        proc.terminate.side_effect = lambda: stopped.append(cmd)
        return proc

    monkeypatch.setattr(subprocess, "Popen", _fake_popen(record))
    result = _run(_hub(tmp_path))
    assert result.exit_code == 1
    assert "could not open port 3801" in " ".join(result.output.split())
    assert stopped and "hubzoid.server:build_app" in stopped[0]


# ---------------------------------------------------------------------------
# Links the bridge writes go through the public port
# ---------------------------------------------------------------------------
def test_a_local_run_gives_the_bridge_its_public_origin(tmp_path, launched):
    result = _run(_hub(tmp_path))
    assert result.exit_code == 0, result.output
    bridge = _bridge(launched)
    assert bridge["HUBZOID_PUBLIC_URL"] == "http://127.0.0.1:3801"     # not the bridge port 3802
    assert bridge["HUBZOID_ALLOWED_ORIGINS"] == "http://localhost:3801"  # the other local spelling
    assert "HUBZOID_PUBLIC_URL" not in _edge(launched)["env"]


def test_localhost_is_kept_as_typed(tmp_path, launched):
    result = _run(_hub(tmp_path, "HUBZOID_ALLOWED_ORIGINS=https://tools.example.com\n"), "--host", "localhost")
    assert result.exit_code == 0, result.output
    bridge = _bridge(launched)
    assert bridge["HUBZOID_PUBLIC_URL"] == "http://localhost:3801"
    assert bridge["HUBZOID_ALLOWED_ORIGINS"] == "https://tools.example.com,http://127.0.0.1:3801"


@pytest.mark.parametrize("env", ["HUBZOID_PUBLIC_URL=https://hub.example.com\n", "WEBUI_URL=https://hub.example.com\n"])
def test_a_configured_public_url_is_kept(tmp_path, launched, env):
    result = _run(_hub(tmp_path, env))
    assert result.exit_code == 0, result.output
    bridge = _bridge(launched)
    assert bridge.get("HUBZOID_PUBLIC_URL", "https://hub.example.com") == "https://hub.example.com"
    assert "HUBZOID_ALLOWED_ORIGINS" not in bridge


def test_a_network_bind_without_a_public_url_says_to_set_one(tmp_path, launched):
    result = _run(_hub(tmp_path, "HUBZOID_AUTH=true\n"), "--host", "0.0.0.0")
    assert result.exit_code == 0, result.output
    assert "HUBZOID_PUBLIC_URL" not in _bridge(launched)
    assert "Set HUBZOID_PUBLIC_URL" in " ".join(result.output.split())


def test_bridge_only_runs_set_no_public_origin(tmp_path, launched):
    assert _run(_hub(tmp_path), "--no-ui").exit_code == 0
    assert "HUBZOID_PUBLIC_URL" not in _bridge(launched)


# ---------------------------------------------------------------------------
# Local mode stays on loopback
# ---------------------------------------------------------------------------
def test_local_mode_refuses_a_network_host(tmp_path, launched):
    result = _run(_hub(tmp_path), "--host", "0.0.0.0")
    assert result.exit_code == 2
    out = " ".join(result.output.split())
    assert "Sign-in is off" in out and "HUBZOID_AUTH=true" in out
    assert "HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true" in out
    assert launched == []


@pytest.mark.parametrize("host", ["192.168.1.20", "::"])
def test_any_non_loopback_host_is_refused(tmp_path, launched, host):
    assert _run(_hub(tmp_path), "--host", host).exit_code == 2
    assert launched == []


@pytest.mark.parametrize("host", ["localhost", "::1", "127.0.0.2"])
def test_loopback_hosts_need_no_sign_in(tmp_path, launched, host):
    result = _run(_hub(tmp_path), "--host", host)
    assert result.exit_code == 0, result.output


def test_network_host_allowed_when_explicitly_accepted(tmp_path, launched):
    hub = _hub(tmp_path, "HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true\n")
    result = _run(hub, "--host", "0.0.0.0")
    assert result.exit_code == 0, result.output
    assert "acts as the hub's owner" in " ".join(result.output.split())
    edge = _edge(launched)
    assert edge["cmd"][edge["cmd"].index("--host") + 1] == "0.0.0.0"


@pytest.mark.parametrize("flag", ["HUBZOID_AUTH=true", "WEBUI_AUTH=true"])
def test_sign_in_allows_a_network_host(tmp_path, launched, flag):
    result = _run(_hub(tmp_path, flag + "\n"), "--host", "0.0.0.0")
    assert result.exit_code == 0, result.output
    assert "sign-in on" in result.output


def test_host_from_the_environment_is_guarded_too(tmp_path, launched, monkeypatch):
    monkeypatch.setenv("HUBZOID_HOST", "0.0.0.0")
    result = CliRunner().invoke(cli.app, ["run", str(_hub(tmp_path)), "--no-open"])
    assert result.exit_code == 2 and launched == []


# ---------------------------------------------------------------------------
# Hosted MCP defaults
# ---------------------------------------------------------------------------
def test_mcp_is_on_for_a_local_run(tmp_path, launched):
    result = _run(_hub(tmp_path))
    assert result.exit_code == 0, result.output
    bridge = _bridge(launched)
    assert bridge["MCP_SERVER"] == "true"
    assert bridge["MCP_PUBLIC_URL"] == "http://127.0.0.1:3801/mcp"
    out = " ".join(result.output.split())
    assert "claude mcp add --transport http ops-desk http://127.0.0.1:3801/mcp" in out


def test_mcp_is_on_for_an_https_public_url(tmp_path, launched):
    hub = _hub(tmp_path, "HUBZOID_AUTH=true\nHUBZOID_PUBLIC_URL=https://hub.example.com/\n")
    result = _run(hub, "--host", "0.0.0.0")
    assert result.exit_code == 0, result.output
    assert _bridge(launched)["MCP_PUBLIC_URL"] == "https://hub.example.com/mcp"
    out = " ".join(result.output.split())
    assert "Hubzoid is ready: https://hub.example.com" in out
    assert "claude mcp add --transport http ops-desk https://hub.example.com/mcp" in out


def test_mcp_stays_off_for_a_plain_http_network_url(tmp_path, launched):
    hub = _hub(tmp_path, "HUBZOID_AUTH=true\nHUBZOID_PUBLIC_URL=http://10.0.0.5:3080\n")
    result = _run(hub, "--host", "0.0.0.0")
    assert result.exit_code == 0, result.output
    assert "MCP_SERVER" not in _bridge(launched)
    assert "claude mcp add" not in result.output


def test_explicit_mcp_settings_win(tmp_path, launched):
    result = _run(_hub(tmp_path, "MCP_SERVER=false\n"))
    assert result.exit_code == 0, result.output
    assert _bridge(launched)["MCP_SERVER"] == "false"
    assert "MCP_PUBLIC_URL" not in _bridge(launched)
    assert "claude mcp add" not in result.output


def test_an_explicit_mcp_url_is_kept(tmp_path, launched):
    result = _run(_hub(tmp_path, "MCP_PUBLIC_URL=https://mcp.example.com/mcp\n"))
    assert result.exit_code == 0, result.output
    assert _bridge(launched)["MCP_SERVER"] == "true"
    assert _bridge(launched)["MCP_PUBLIC_URL"] == "https://mcp.example.com/mcp"
    assert "https://mcp.example.com/mcp" in result.output


@pytest.mark.parametrize("env,origin,expected", [
    ({}, "http://127.0.0.1:3080", {"MCP_SERVER": "true", "MCP_PUBLIC_URL": "http://127.0.0.1:3080/mcp"}),
    ({}, "http://[::1]:3080", {"MCP_SERVER": "true", "MCP_PUBLIC_URL": "http://[::1]:3080/mcp"}),
    ({}, "https://hub.example.com", {"MCP_SERVER": "true", "MCP_PUBLIC_URL": "https://hub.example.com/mcp"}),
    ({}, "http://127.0.0.2:3080", {}),        # loopback, but not one MCP OAuth accepts
    ({}, "http://hub.internal:3080", {}),
    ({}, "", {}),
    ({"MCP_SERVER": "true"}, "https://h.example", {"MCP_PUBLIC_URL": "https://h.example/mcp"}),
    ({"MCP_SERVER": ""}, "https://h.example", {}),   # set, even empty: explicit
    ({"MCP_SERVER": "true", "MCP_PUBLIC_URL": "https://x.example/mcp"}, "https://h.example", {}),
])
def test_mcp_defaults(env, origin, expected):
    assert cli._mcp_defaults(env, origin) == expected


# ---------------------------------------------------------------------------
# Upgrading from Open WebUI
# ---------------------------------------------------------------------------
def _owui_db(hub: Path, people: int = 2) -> Path:
    path = hub / ".openwebui-data" / "webui.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT, role TEXT)')
        for i in range(people):
            con.execute('INSERT INTO "user" VALUES (?, ?, ?)', (f"u{i}", f"p{i}@example.com", "user"))
    return path


def test_sign_in_with_an_unmigrated_open_webui_install_stops(tmp_path, launched):
    hub = _hub(tmp_path, "HUBZOID_AUTH=true\n")
    _owui_db(hub)
    result = _run(hub)
    assert result.exit_code == 1
    out = " ".join(result.output.split())
    assert "2 account(s)" in out and "no Hubzoid accounts yet" in out
    assert _flat(f"hubzoid migrate openwebui {hub}") in _flat(result.output)
    assert "dry run first" in out and _flat(f"hubzoid backup {hub}") in _flat(result.output)
    assert "stop the hub, and apply" in out
    assert _flat('pip install "hubzoid[openwebui]"') in _flat(result.output)
    assert "HUBZOID_UI=openwebui" in out
    assert launched == []


def test_local_mode_says_old_chats_can_be_imported(tmp_path, launched):
    hub = _hub(tmp_path)
    _owui_db(hub)
    result = _run(hub)
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "can be imported" in out and "hubzoid migrate openwebui" in out


def test_a_migrated_hub_starts_with_sign_in(tmp_path, launched):
    hub = _hub(tmp_path, "HUBZOID_AUTH=true\n")
    _owui_db(hub)
    (hub / ".hubzoid").mkdir()
    with sqlite3.connect(hub / ".hubzoid" / "hub.db") as con:  # the standalone operational store
        con.execute("CREATE TABLE hz_users (id TEXT PRIMARY KEY, email TEXT)")
        con.execute("INSERT INTO hz_users VALUES ('u0', 'p0@example.com')")
    result = _run(hub)
    assert result.exit_code == 0, result.output
    assert "can be imported" not in result.output


def test_an_empty_open_webui_database_is_not_an_install(tmp_path, launched):
    hub = _hub(tmp_path, "HUBZOID_AUTH=true\n")
    _owui_db(hub, people=0)
    result = _run(hub)
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Open WebUI mode
# ---------------------------------------------------------------------------
def test_legacy_mode_without_the_extra_explains_how_to_install_it(tmp_path, launched, monkeypatch):
    monkeypatch.setattr(webui, "_find_binary", lambda: None)
    result = _run(_hub(tmp_path, "HUBZOID_UI=openwebui\n"))
    assert result.exit_code == 1
    assert _flat('pip install "hubzoid[openwebui]"') in _flat(result.output)
    assert "not installed" in " ".join(result.output.split())
    assert launched == []


def test_legacy_bridge_only_needs_no_extra(tmp_path, launched, monkeypatch):
    monkeypatch.setattr(webui, "_find_binary", lambda: None)
    result = _run(_hub(tmp_path, "HUBZOID_UI=openwebui\n"), "--no-ui")
    assert result.exit_code == 0, result.output


def test_legacy_mode_keeps_open_webui_behind_the_edge(tmp_path, launched):
    result = _run(_hub(tmp_path, "HUBZOID_UI=openwebui\nMCP_SERVER=true\nMCP_PUBLIC_URL=http://127.0.0.1:3801/mcp\n"))
    assert result.exit_code == 0, result.output
    assert [c for c in launched if c["cmd"][0] == "/fake/open-webui"]
    edge = _edge(launched)["env"]
    assert edge["HUBZOID_EDGE_DEFAULT"] == f"http://127.0.0.1:{cli._owui_internal_port(3801)}"
    prefixes = [r["prefix"] for r in json.loads(edge["HUBZOID_EDGE_ROUTES"])]
    assert prefixes[:3] == ["/artifacts", "/portal", "/mcp"]
    assert _bridge(launched)["OWUI_INTERNAL_URL"] == f"http://127.0.0.1:{cli._owui_internal_port(3801)}"
    assert "Hubzoid is ready" not in result.output


def test_legacy_mode_may_bind_the_network_without_hubzoid_sign_in(tmp_path, launched):
    """1.0.x behaviour: Open WebUI has its own sign-in settings."""
    result = _run(_hub(tmp_path, "HUBZOID_UI=openwebui\n"), "--host", "0.0.0.0")
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Opening the browser
# ---------------------------------------------------------------------------
def test_browser_opens_on_the_ready_url(tmp_path, launched, monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "_should_open_browser", lambda host, no_open: True)
    monkeypatch.setattr(cli, "_open_browser", opened.append)
    result = CliRunner().invoke(cli.app, ["run", str(_hub(tmp_path)), "--port", "3801",
                                          "--bridge-port", "3802"])
    assert result.exit_code == 0, result.output
    assert opened == ["http://127.0.0.1:3801"]


def test_when_the_browser_opens(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    for key in ("BROWSER", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cli._should_open_browser("127.0.0.1", False)
    assert cli._should_open_browser("localhost", False)
    assert not cli._should_open_browser("127.0.0.1", True)          # --no-open
    assert not cli._should_open_browser("0.0.0.0", False)            # not loopback
    monkeypatch.setattr(sys, "platform", "linux")
    assert not cli._should_open_browser("127.0.0.1", False)          # no graphical session
    monkeypatch.setenv("DISPLAY", ":0")
    assert cli._should_open_browser("127.0.0.1", False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert not cli._should_open_browser("127.0.0.1", False)          # not a terminal


# ---------------------------------------------------------------------------
# The default path never imports Open WebUI
# ---------------------------------------------------------------------------
_NO_OWUI = textwrap.dedent('''
    import importlib.abc, subprocess, sys
    from unittest.mock import MagicMock

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "open_webui" or name.startswith("open_webui."):
                raise ImportError("blocked " + name)

    sys.meta_path.insert(0, Block())

    def fake_popen(cmd, *a, **kw):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        return proc

    from typer.testing import CliRunner
    import hubzoid.edge, hubzoid.server, hubzoid.webapp, hubzoid.doctor  # noqa: F401
    from hubzoid import cli
    subprocess.Popen = fake_popen
    cli._wait_for = lambda *a, **k: True
    cli._wait_any = lambda *a, **k: None
    cli._stop_groups = cli._stop_processes
    cli._ensure_port_available = lambda *a: None
    cli.signal.signal = lambda *a, **k: None
    result = CliRunner().invoke(cli.app, ["run", sys.argv[1], "--port", "3811",
                                          "--bridge-port", "3812", "--no-open"])
    print("EXIT", result.exit_code)
    print("LOADED", sorted(m for m in sys.modules if m.split(".")[0] == "open_webui"))
''')


def test_the_default_path_never_imports_open_webui(tmp_path):
    hub = _hub(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-c", _NO_OWUI, str(hub)], capture_output=True,
                          text=True, timeout=120, env=env, cwd=tmp_path)
    assert "EXIT 0" in proc.stdout, proc.stdout + proc.stderr[-3000:]
    assert "LOADED []" in proc.stdout


_LIGHT = textwrap.dedent('''
    import subprocess, sys
    from unittest.mock import MagicMock
    import hubzoid, hubzoid.edge  # noqa: F401  (what the edge process imports)
    edge_modules = sorted(m for m in ("agents", "litellm", "hubzoid.factory", "dbos") if m in sys.modules)
    from typer.testing import CliRunner
    from hubzoid import cli

    class FakePopen:
        def __class_getitem__(cls, item):
            return cls

        def __new__(cls, *a, **kw):
            proc = MagicMock()
            proc.poll.return_value = None
            proc.wait.return_value = 0
            return proc

    subprocess.Popen = FakePopen
    cli._wait_for = lambda *a, **k: True
    cli._wait_any = lambda *a, **k: None
    cli._stop_groups = cli._stop_processes
    cli._ensure_port_available = lambda *a: None
    result = CliRunner().invoke(cli.app, ["run", sys.argv[1], "--port", "3813",
                                          "--bridge-port", "3814", "--no-open"])
    print("EXIT", result.exit_code)
    print("EDGE", edge_modules)
    print("RUN", sorted(m for m in ("agents", "litellm", "hubzoid.factory", "hubzoid.access") if m in sys.modules))
''')


def test_run_and_the_edge_start_without_the_agent_sdks(tmp_path):
    """The run supervisor and the edge never load the agent SDKs: only the bridge
    needs them, so they add nothing to the time before the page answers."""
    hub = _hub(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-c", _LIGHT, str(hub)], capture_output=True,
                          text=True, timeout=120, env=env, cwd=tmp_path)
    assert "EXIT 0" in proc.stdout, proc.stdout + proc.stderr[-3000:]
    assert "EDGE []" in proc.stdout and "RUN []" in proc.stdout, proc.stdout


def test_build_agent_is_still_exported():
    import hubzoid
    from hubzoid.factory import build_agent

    assert hubzoid.build_agent is build_agent


def test_a_bridge_that_exits_is_reported_at_once(tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(cli, "_wait_for", lambda url, timeout=60.0: waits.append(url) or False)
    proc = MagicMock()
    proc.poll.return_value = 1  # exited
    assert cli._wait_for_bridge(proc, "http://127.0.0.1:3802/healthz", timeout=60) is False
    assert not waits


def test_a_slow_bridge_is_waited_for(tmp_path, monkeypatch):
    answers = iter([False, False, True])
    monkeypatch.setattr(cli, "_wait_for", lambda url, timeout=60.0: next(answers))
    proc = MagicMock()
    proc.poll.return_value = None  # still starting
    assert cli._wait_for_bridge(proc, "http://127.0.0.1:3802/healthz", timeout=60) is True


@pytest.mark.parametrize("dead,code", [("hubzoid.server:build_app", 1),
                                       ("hubzoid.server:build_app", 0),
                                       ("hubzoid.edge:_factory", 1),
                                       ("hubzoid.edge:_factory", 0)])
def test_service_exit_after_readiness_is_a_failure(tmp_path, monkeypatch, launched, dead, code):
    original = subprocess.Popen

    def failing_bridge(cmd, *args, **kwargs):
        proc = original(cmd, *args, **kwargs)
        if dead in cmd:
            proc.poll.side_effect = [None, None, code, code]
        return proc

    monkeypatch.setattr(subprocess, "Popen", failing_bridge)
    monkeypatch.setattr(cli, "_wait_any", _WAIT_ANY)
    assert _run(_hub(tmp_path)).exit_code == 1


@pytest.mark.slow
@pytest.mark.parametrize("option", ["--bridge-port", "--port"])
def test_occupied_port_refuses_start_without_launching_children(tmp_path, monkeypatch, launched, option):
    import socket

    # Undo the default test's port-probe stub for this actual occupied socket.
    monkeypatch.undo()
    with clean_process_env(), socket.socket() as occupied, socket.socket() as available:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        available.bind(("127.0.0.1", 0))
        other_port = available.getsockname()[1]
        available.close()
        started = []
        monkeypatch.setattr(subprocess, "Popen", _fake_popen(lambda *a: started.append(a)))
        other_option = "--port" if option == "--bridge-port" else "--bridge-port"
        result = CliRunner().invoke(cli.app, ["run", str(_hub(tmp_path)), "--no-open", option, str(port),
                                             other_option, str(other_port)])
    assert result.exit_code == 1, result.output
    assert "unavailable" in result.output and option in result.output
    assert not started


@pytest.mark.parametrize("code", [0, 1])
def test_openwebui_exit_after_readiness_fails_the_standalone_run(tmp_path, monkeypatch, launched, code):
    original = subprocess.Popen

    def failing_ui(cmd, *args, **kwargs):
        proc = original(cmd, *args, **kwargs)
        if cmd[0] == "/fake/open-webui":
            proc.poll.side_effect = [None, None, code, code, code]
        return proc

    monkeypatch.setattr(subprocess, "Popen", failing_ui)
    monkeypatch.setattr(cli, "_wait_any", _WAIT_ANY)
    result = _run(_hub(tmp_path, "HUBZOID_UI=openwebui\n"))
    assert result.exit_code == 1, result.output


@pytest.mark.parametrize("mode", ["hubzoid", "openwebui"])
def test_public_service_readiness_timeout_is_a_failure(tmp_path, monkeypatch, launched, mode):
    def readiness(proc, url, **kwargs):
        return kwargs.get('label', 'bridge') == 'bridge'

    monkeypatch.setattr(cli, '_wait_for_bridge', readiness)
    result = _run(_hub(tmp_path, f"HUBZOID_UI={mode}\n"))
    assert result.exit_code == 1, result.output
    assert 'Hubzoid is ready' not in result.output


@pytest.mark.slow
def test_recently_closed_tcp_connections_do_not_block_restart():
    import socket

    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        listener.listen()
        with socket.create_connection(('127.0.0.1', port), timeout=3) as client:
            connection, _ = listener.accept()
            connection.close()  # server is the active closer: its port enters TIME_WAIT
            assert client.recv(1) == b''
    cli._ensure_port_available('127.0.0.1', port, 'Bridge', '--bridge-port')
    with socket.socket() as restarted:
        restarted.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        restarted.bind(('127.0.0.1', port))
        restarted.listen()
