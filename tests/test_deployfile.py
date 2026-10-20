"""hubzoid.yaml: the deployment file, and `hubzoid run` / `gateway` using it."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from hubzoid import cli, deployfile
from tests._fake_secrets import clean_process_env


def _hub(root, name, port):
    hub = root / name
    hub.mkdir(parents=True)
    (hub / "AGENTS.md").write_text(f"---\nname: {name}\n---\nbody")
    (hub / ".env").write_text(f"BRIDGE_PORT={port}\nBRIDGE_API_KEYS=k-{name}-long-enough\n")
    return hub


def test_errors_name_the_line(tmp_path):
    (tmp_path / "hubzoid.yaml").write_text("version: 1\nhubs:\n  - nope\nsign_in: yes please\n")
    with pytest.raises(deployfile.DeployFileError) as exc:
        deployfile.load(tmp_path / "hubzoid.yaml")
    assert "line" in str(exc.value)
    (tmp_path / "hubzoid.yaml").write_text("version: 1\ncolour: red\n")
    with pytest.raises(deployfile.DeployFileError, match="unknown key 'colour'"):
        deployfile.load(tmp_path / "hubzoid.yaml")


def test_settings_fill_from_the_file_and_a_disagreement_stops(tmp_path):
    _hub(tmp_path, "ops", 3811)
    (tmp_path / "hubzoid.yaml").write_text("version: 1\npublic_url: https://hubs.example.org/\nsign_in: true\n"
                                           "owner: Owner@Example.org\nhubs: [ops]\nbrand:\n  identity:\n"
                                           "    name: Example Hubs\n")
    df = deployfile.load(tmp_path / "hubzoid.yaml")
    assert df.name == "Example Hubs" and df.hub_dirs() == [(tmp_path / "ops").resolve()]
    values, problems = deployfile.environment(df, {})
    assert values == {"HUBZOID_PUBLIC_URL": "https://hubs.example.org", "HUBZOID_AUTH": "true",
                      "HUBZOID_ADMIN_EMAIL": "owner@example.org"} and problems == []
    _, problems = deployfile.environment(df, {"HUBZOID_AUTH": "false"})
    assert problems and "sign_in" in problems[0]
    assert deployfile.owning(tmp_path / "ops") == (tmp_path / "hubzoid.yaml").resolve()


@pytest.fixture
def launched(monkeypatch):
    calls = []

    class Popen:  # a class, so `subprocess.Popen[bytes]` annotations still work
        def __class_getitem__(cls, item):
            return cls

        def __new__(cls, cmd, *a, env=None, **kw):
            calls.append({"cmd": [str(c) for c in cmd], "env": dict(env or {})})
            proc = MagicMock()
            proc.poll.return_value = None
            return proc

    monkeypatch.setattr(cli.subprocess, "Popen", Popen)
    monkeypatch.setattr(cli, "_wait_for", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_ensure_port_available", lambda *a: None)
    monkeypatch.setattr(cli, "_wait_any", lambda procs, **k: None)
    monkeypatch.setattr(cli, "_stop_groups", lambda procs, **k: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *a, **k: None)
    with clean_process_env():
        yield calls


def test_run_with_no_argument_runs_the_deployment_file(tmp_path, launched, monkeypatch):
    _hub(tmp_path, "sales", 3821), _hub(tmp_path, "support", 3822)
    (tmp_path / "hubzoid.yaml").write_text("version: 1\npublic_url: https://hubs.example.org\n"
                                           "hubs: [sales, support]\nbrand:\n  identity:\n    name: Acme\n")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, ["run", "--port", "3820", "--no-open"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    bridges = [c for c in launched if c["cmd"][1:4] == ["-m", "hubzoid", "run"]]
    assert [c["cmd"][4] for c in bridges] == [str((tmp_path / h).resolve()) for h in ("sales", "support")]
    assert bridges[0]["env"]["HUBZOID_PUBLIC_URL"] == "https://hubs.example.org/b/sales"
    manifest = json.loads((tmp_path / ".hubzoid" / "deployment.json").read_text())
    assert manifest["name"] == "Acme" and manifest["public_url"] == "https://hubs.example.org"
    table = json.loads((tmp_path / ".hubzoid" / "routes.json").read_text())
    assert table["states"] == {"sales": "ready", "support": "ready"}
    edge = next(c for c in launched if "hubzoid.edge:_factory" in c["cmd"])
    assert edge["env"]["HUBZOID_EDGE_TABLE"] == str((tmp_path / ".hubzoid" / "routes.json").resolve())


def test_gateway_is_an_alias_that_prints_the_file(tmp_path, launched, monkeypatch):
    _hub(tmp_path, "sales", 3831), _hub(tmp_path, "support", 3832)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, ["gateway", "sales", "support", "--port", "3830",
                                          "--name", "Acme", "--data-dir", "gw"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "hubzoid run" in result.output and "  - sales" in result.output and "name: Acme" in result.output
    no = CliRunner().invoke(cli.app, ["gateway", "sales", "--no-bridges"])
    assert no.exit_code == 2 and "hubzoid restart" in " ".join(no.output.split())


def test_a_broken_policy_section_stops_the_start(tmp_path, launched, monkeypatch):
    _hub(tmp_path, "sales", 3841), _hub(tmp_path, "support", 3842)
    (tmp_path / "hubzoid.yaml").write_text("version: 1\nhubs: [sales, support]\n"
                                           "chat:\n  sharing:\n    enabeld: false\n")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, ["run", "--port", "3840", "--no-open"])
    assert result.exit_code == 2 and "chat:" in result.output and not launched
