"""A hub argument given as a deployment folder (``hubzoid._hubarg``)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from hubzoid import _hubarg, deployment


def _deployment(root: Path, hubs: list[str]) -> Path:
    for name in hubs:
        (root / name).mkdir()
        (root / name / "AGENTS.md").write_text("---\nname: x\n---\nhi\n")
    (root / "hubzoid.yaml").write_text("version: 1\nhubs:\n" + "".join(f"  - {h}\n" for h in hubs))
    return root


def test_a_deployment_of_one_hub_means_that_hub(tmp_path):
    root = _deployment(tmp_path, ["ops"])
    assert _hubarg.resolve(root) == (root / "ops").resolve()


def test_several_hubs_ask_which_one_unless_the_command_spans_the_deployment(tmp_path):
    root = _deployment(tmp_path, ["ops", "Desk"])
    with pytest.raises(typer.BadParameter, match=r"runs 2 hubs \(ops, Desk\).*hubzoid audit ops"):
        _hubarg.resolve(root, command="audit")
    assert _hubarg.resolve(root, deployment_wide=True) == (root / "ops").resolve()


def test_a_hub_folder_is_left_alone(tmp_path):
    root = _deployment(tmp_path, ["ops"])
    assert _hubarg.resolve(root / "ops") == root / "ops"


def test_the_root_group_resolves_every_sub_command(tmp_path, monkeypatch):
    root = _deployment(tmp_path, ["ops", "desk"])
    app = typer.Typer(cls=_hubarg.HubArgGroup)
    seen = {}
    admin = typer.Typer()
    app.add_typer(admin, name="admin")

    @admin.command("list")
    def admin_list(hub: Path = typer.Argument(Path("."))):
        seen["admin"] = hub

    @app.command("audit")
    def audit(hub: Path = typer.Argument(Path("."))):
        seen["audit"] = hub

    monkeypatch.chdir(root)
    runner = CliRunner()
    assert runner.invoke(app, ["admin", "list"]).exit_code == 0
    assert seen["admin"] == (root / "ops").resolve()
    out = runner.invoke(app, ["audit"])
    assert out.exit_code == 2 and "Name one" in out.output
    assert runner.invoke(app, ["audit", "desk"]).exit_code == 0


def test_the_deployment_data_folder_holds_the_manifest_itself(tmp_path):
    state = tmp_path / ".hubzoid"
    state.mkdir()
    (state / "deployment.json").write_text(json.dumps({"version": 1, "hubs": []}))
    assert deployment.manifest_path(tmp_path, env={}) == state / "deployment.json"
