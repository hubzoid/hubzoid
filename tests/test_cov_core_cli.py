"""CLI deployment refusals, control output and child cleanup without services."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer
from typer.testing import CliRunner

from hubzoid import cli
from tests.test_cli_run_webapp import _hub, _isolated  # noqa: F401
from tests.test_supervisor import Proc


def _invoke(*args):
    return CliRunner().invoke(cli.app, list(map(str, args)))


def test_control_status_shows_retry_failure_and_reload_reason(tmp_path, monkeypatch):
    from hubzoid import supervisor

    monkeypatch.setattr(supervisor, "request", lambda state, payload: {"ok": True, "units": [
        {"hub": "ops", "state": "restarting", "since": 100, "restarts": 3,
         "retry_in": 5, "error": "process exited", "reload_needed": ["AGENTS.md changed"]},
        {"hub": "idle", "state": "stopped", "since": None, "restarts": 0}]})
    result = _invoke("status", "--data-dir", tmp_path)
    assert result.exit_code == 0, result.output
    assert "again in 5 s" in result.output and "process exited" in result.output
    assert "reload needed: AGENTS.md changed" in result.output
    assert "idle" in result.output and "stopped" in result.output
    result = _invoke("status", "--data-dir", tmp_path, "--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["units"][0]["restarts"] == 3


@pytest.mark.parametrize("operation", ["restart", "reload", "stop", "start"])
def test_control_commands_send_target_and_render_new_state(tmp_path, monkeypatch, operation):
    from hubzoid import supervisor

    requests = []

    def request(state, payload):
        requests.append((state, payload))
        return {"ok": True, "results": [{"hub": "ops", "ok": True, "state": "ready"}]}

    monkeypatch.setattr(supervisor, "request", request)
    result = _invoke(operation, "ops", "--data-dir", tmp_path)
    assert result.exit_code == 0 and "ops: ready" in result.output
    assert requests[0][0] == tmp_path.resolve()
    assert requests[0][1]["op"] == operation and requests[0][1]["hubs"] == ["ops"]


@pytest.mark.parametrize(("response", "message"), [
    ({"ok": False, "error": "not running"}, "not running"),
    ({"ok": False, "results": [{"hub": "ops", "problems": ["invalid tools"]}]}, "not restarted"),
    ({"ok": False, "results": [{"hub": "ops", "ok": False, "state": "failed", "error": "spawn denied"}]}, "failed (spawn denied)"),
])
def test_control_failure_exit_and_details(tmp_path, monkeypatch, response, message):
    from hubzoid import supervisor

    monkeypatch.setattr(supervisor, "request", lambda *a: response)
    result = _invoke("reload", "ops", "--data-dir", tmp_path)
    assert result.exit_code == 1 and message in result.output
    result = _invoke("reload", "ops", "--data-dir", tmp_path, "--json")
    assert result.exit_code == 1 and json.loads(result.stdout) == response


def test_control_connection_error_and_log_tail(tmp_path, monkeypatch):
    from hubzoid import supervisor

    monkeypatch.setattr(supervisor, "request", Mock(side_effect=ConnectionError("control socket missing")))
    result = _invoke("status", "--data-dir", tmp_path)
    assert result.exit_code == 1 and "control socket missing" in result.output
    log = tmp_path / "ops.log"
    log.write_text("older\n")
    monkeypatch.setattr(supervisor, "request", lambda *a: {"ok": True, "lines": ["tail line"], "path": str(log)})
    monkeypatch.setattr(cli.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    result = _invoke("logs", "ops", "--follow", "--data-dir", tmp_path)
    assert result.exit_code == 0 and result.output.strip() == "tail line"


def test_find_state_dir_searches_parent_hub_and_deployment(tmp_path, monkeypatch):
    from hubzoid import supervisor

    hub = _hub(tmp_path)
    state = hub / ".hubzoid"
    state.mkdir()
    supervisor.control_file(state).write_text("{}")
    child = hub / "subdir"
    child.mkdir()
    monkeypatch.chdir(child)
    assert cli._find_state_dir(None) == state
    supervisor.control_file(state).unlink()
    assert cli._find_state_dir(None) == child / ".hubzoid"
    (hub / "hubzoid.yaml").write_text("hubs: []\n")
    gateway = hub / ".hubzoid-gateway"
    gateway.mkdir()
    supervisor.control_file(gateway).write_text("{}")
    assert cli._find_state_dir(None) == gateway


@pytest.mark.parametrize(("args", "message"), [
    (["--bridge-port", "1234"], "--bridge-port is for one hub"),
    (["--no-ui"], "--no-ui runs one hub"),
    (["--behind", "https://example.test"], "--behind runs one hub"),
])
def test_multihub_flags_refused_before_start(tmp_path, args, message):
    one = _hub(tmp_path, name="one")
    two = _hub(tmp_path, name="two")
    result = _invoke("run", one, two, *args)
    assert result.exit_code == 2 and message in result.output


def test_run_empty_or_invalid_deployfile_refuses(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "hubzoid.yaml"
    config.write_text("version: 1\nhubs: []\n")
    result = _invoke("run")
    assert result.exit_code == 2 and "lists no hubs" in result.output
    config.write_text("version: 99\nhubs: []\n")
    result = _invoke("run")
    assert result.exit_code == 2 and "version" in result.output.lower()


def test_missing_hub_names_current_hub_tip(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    monkeypatch.chdir(hub)
    result = _invoke("run", "missing")
    assert result.exit_code == 2
    assert "looks like a hub" in result.output and "hubzoid run ." in result.output


def test_check_hub_collects_independent_load_errors(tmp_path, monkeypatch):
    from hubzoid.channels import config
    from hubzoid.loaders import agents, tools_local

    hub = _hub(tmp_path)
    monkeypatch.setattr(cli.settingslib, "load", Mock(side_effect=ValueError("bad settings")))
    monkeypatch.setattr(agents, "load_main", Mock(side_effect=ValueError("bad instructions")))
    monkeypatch.setattr(tools_local, "load_all", Mock(side_effect=RuntimeError("bad tools")))
    monkeypatch.setattr(config, "files", Mock(side_effect=OSError("bad channel folder")))
    result = _invoke("check-hub", hub)
    assert result.exit_code == 1
    assert ".env: bad settings" in result.output
    assert "AGENTS.md: bad instructions" in result.output
    assert "tools_local: RuntimeError: bad tools" in result.output
    assert "channels: bad channel folder" in result.output


def test_check_hub_reports_invalid_channel_file(tmp_path):
    hub = _hub(tmp_path)
    (hub / "channels").mkdir()
    (hub / "channels" / "bad.yaml").write_text("version: 99\nid: bad\nprovider: unknown\n")
    result = _invoke("check-hub", hub)
    assert result.exit_code == 1 and "channels/bad.yaml: Input should be 1" in result.output


@pytest.mark.parametrize(("exit_code", "output", "expected"), [
    (0, "ignored", []), (2, "line1\n\nline2", ["line1", "line2"]),
    (3, "", ["check failed (status 3)"]),
])
def test_reload_validation_reports_child_check_failure(tmp_path, monkeypatch, exit_code, output, expected):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=exit_code, stdout=output, stderr=""))
    unit = SimpleNamespace(hub_dir=tmp_path, specs=[])
    assert cli._validate_hub_unit(unit) == expected
    unit.hub_dir = None
    assert cli._validate_hub_unit(unit) == []


@pytest.mark.parametrize("error", [OSError("denied"), subprocess.TimeoutExpired("check", 120)])
def test_reload_validation_handles_spawn_and_timeout(tmp_path, monkeypatch, error):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))
    assert cli._validate_hub_unit(SimpleNamespace(hub_dir=tmp_path, specs=[])) == [f"could not check the hub: {error}"]


@pytest.mark.parametrize(("service", "healthy"), [(True, True), (True, False), (False, True), (False, False)])
def test_bridge_child_is_reaped_on_success_and_failed_readiness(tmp_path, monkeypatch, service, healthy):
    from hubzoid import db, supervisor

    monkeypatch.setattr(db, "operational_url", lambda hub: "sqlite:///unused")
    monkeypatch.setattr(supervisor, "watch_parent", lambda: None)
    monkeypatch.setattr(cli, "_wait_for_bridge", lambda *a, **kw: healthy)
    procs = []

    def popen(cmd, **kw):
        proc = Proc(cmd)
        procs.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(cli, "_channel_specs", lambda *a: [SimpleNamespace(cmd=["channel"], env={})])
    run = cli._run_hub_service if service else cli._run_hub_process
    kwargs = {"br_port": 1234, **({"bridge_host": "::"} if service else {})}
    if healthy:
        run(tmp_path, ["bridge"], {}, **kwargs)
    else:
        with pytest.raises(typer.Exit) as exc:
            run(tmp_path, ["bridge"], {}, **kwargs)
        assert exc.value.exit_code == 1
    assert len(procs) == (2 if service and healthy else 1)
    assert all(p.signals == ["TERM"] and p.code == -15 for p in procs)


def test_windows_process_stop_delegates_to_platform_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(cli, "_stop_groups", lambda procs, **kw: calls.append((procs, kw)))
    cli._stop_processes(["child"], timeout=3)
    assert calls == [(["child"], {"timeout": 3})]


def test_bridge_readiness_rejects_exited_child_and_reports_slow_start(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_wait_for", lambda *a, **kw: False)
    times = iter([0, 0, 1, 25, 26, 40])
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: next(times)))
    assert cli._wait_for_bridge(SimpleNamespace(poll=lambda: None), "http://fake", timeout=30) is False
    assert "still starting" in capsys.readouterr().out
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: 0))
    assert cli._wait_for_bridge(SimpleNamespace(poll=lambda: 1), "http://fake", timeout=1) is False


def test_deployment_secret_is_private_persistent_and_unwritable_is_empty(tmp_path):
    secret = cli._deployment_bridge_key(tmp_path)
    assert len(secret) >= 32
    assert cli._deployment_bridge_key(tmp_path) == secret
    key = tmp_path / "bridge.key"
    assert key.is_file() and key.stat().st_mode & 0o077 == 0
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    assert cli._deployment_bridge_key(blocked) == ""


def test_backup_failure_and_verify_warning_rendering(tmp_path, monkeypatch):
    from hubzoid import backup

    monkeypatch.setattr(backup, "backup", Mock(side_effect=backup.BackupError("backup refused")))
    result = _invoke("backup", tmp_path)
    assert result.exit_code == 1 and "backup refused" in result.output
    archive = tmp_path / "backup.tar.gz"
    archive.write_bytes(b"fixture")
    monkeypatch.setattr(backup, "verify", lambda path: {"ok": False, "files": 2, "databases": 1,
        "postgres": 1, "problems": ["checksum mismatch"], "warnings": ["restore tool missing"]})
    result = _invoke("backup", "verify", archive)
    assert result.exit_code == 1 and "Not verified" in result.output
    assert "checksum mismatch" in result.output and "restore tool missing" in result.output
    assert "1 PostgreSQL" in result.output


def test_backup_list_renders_archives(tmp_path, monkeypatch):
    from hubzoid import backup_schedule

    monkeypatch.setattr(backup_schedule, "archives", lambda hub: [
        {"created": "2026-11-22", "bytes": 2_000_000, "path": "backup.tar.gz"}])
    monkeypatch.setattr(backup_schedule, "folder", lambda hub: tmp_path / "backups")
    result = _invoke("backup", "list", tmp_path)
    assert result.exit_code == 0 and "2026-11-22" in result.output and "2.0 MB" in result.output


def test_upgrade_requires_a_hub_and_refuses_live_deployment(tmp_path, monkeypatch):
    from hubzoid import supervisor

    result = _invoke("upgrade", tmp_path)
    assert result.exit_code == 2 and "No hubs found" in result.output
    hub = _hub(tmp_path)
    monkeypatch.setattr(supervisor.Supervisor, "lock", Mock(side_effect=RuntimeError("locked")))
    result = _invoke("upgrade", hub)
    assert result.exit_code == 2 and "This deployment is running" in result.output


def test_version_and_installed_version_fallback(monkeypatch):
    import importlib.metadata

    result = _invoke("--version")
    assert result.exit_code == 0 and cli.__version__ in result.output
    monkeypatch.setattr(importlib.metadata, "version", Mock(side_effect=importlib.metadata.PackageNotFoundError))
    assert cli._installed_version() == cli.__version__


@pytest.mark.real_logins
def test_model_selection_reprompts_invalid_choice(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_available_local_models", lambda: ["claude-local", "codex-local"])
    monkeypatch.setattr(typer, "prompt", Mock(side_effect=[0, 3, 2]))
    assert cli._choose_initial_model() == "codex-local"
    output = capsys.readouterr().out
    assert output.count("Choose one of the listed numbers.") == 2


def test_hub_model_uses_shell_if_secret_unreadable(tmp_path, monkeypatch):
    from hubzoid import config_secrets

    monkeypatch.setenv("MODEL", "codex-local/gpt-5")
    monkeypatch.setattr(config_secrets, "resolve_key", Mock(side_effect=OSError("unavailable")))
    assert cli._hub_model(tmp_path) == "codex-local/gpt-5"
