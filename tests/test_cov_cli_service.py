"""`hubzoid service install`: the systemd unit it prints for a deployment
(working folder, the exact `hubzoid run` command, restart policy, the user),
without writing anything outside the test's own folder."""
from __future__ import annotations

from typer.testing import CliRunner

from hubzoid.cli import app


def test_the_unit_runs_the_deployment_and_restarts_it(tmp_path):
    folder = tmp_path / "deploy"
    folder.mkdir()
    state = tmp_path / "state dir"
    r = CliRunner().invoke(app, ["service", "install", str(folder), "--port", "8080", "--host", "0.0.0.0",
                                 "--data-dir", str(state), "--user", "hubzoid"], env={"COLUMNS": "250"})
    assert r.exit_code == 0, r.output
    unit = r.output
    assert f"WorkingDirectory={folder.resolve()}" in unit
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert exec_line.endswith(f"run --port 8080 --host 0.0.0.0 --no-open --data-dir '{state.resolve()}'")
    assert "Restart=always" in unit and "KillMode=mixed" in unit and "User=hubzoid" in unit
    assert "WantedBy=multi-user.target" in unit and "systemctl daemon-reload" in unit


def test_without_a_user_the_unit_names_none(tmp_path):
    r = CliRunner().invoke(app, ["service", "install", str(tmp_path)])
    assert r.exit_code == 0 and "User=" not in r.output
    assert "--port 3080 --host 127.0.0.1 --no-open" in r.output and "--data-dir" not in r.output
