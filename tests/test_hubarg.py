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


def test_a_hub_command_gets_the_deployments_public_url(tmp_path, monkeypatch):
    """Session B: `hubzoid channels github connect hubs/x` asked for
    HUBZOID_PUBLIC_URL though hubzoid.yaml sets public_url."""
    import os

    root = tmp_path
    (root / "hubs" / "ops").mkdir(parents=True)
    (root / "hubs" / "ops" / "AGENTS.md").write_text("hi")
    (root / "hubzoid.yaml").write_text("version: 1\npublic_url: https://hub.example.com\nhubs:\n  - hubs/ops\n")
    monkeypatch.delenv("HUBZOID_PUBLIC_URL", raising=False)
    _hubarg.deployment_environment(root / "hubs" / "ops")
    assert os.environ["HUBZOID_PUBLIC_URL"] == "https://hub.example.com"
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://other.example.com")   # the environment wins
    _hubarg.deployment_environment(root / "hubs" / "ops")
    assert os.environ["HUBZOID_PUBLIC_URL"] == "https://other.example.com"


def test_a_hub_option_resolves_too_and_embed_spans_the_deployment(tmp_path, monkeypatch):
    """Found in the HUB-140 proof: `hubzoid embed secret` (a --hub option) said
    ". is not a hub folder" in a deployment folder."""
    root = _deployment(tmp_path, ["ops", "desk"])
    app = typer.Typer(cls=_hubarg.HubArgGroup)
    seen = {}
    embed = typer.Typer()
    app.add_typer(embed, name="embed")

    @embed.command("list")
    def embed_list(hub: Path = typer.Option(Path("."), "--hub")):
        seen["embed"] = hub

    @app.command("chat")
    def chat(hub: Path = typer.Option(Path("."), "--hub")):
        seen["chat"] = hub

    monkeypatch.chdir(root)
    runner = CliRunner()
    assert runner.invoke(app, ["embed", "list"]).exit_code == 0
    assert seen["embed"] == (root / "ops").resolve()
    out = runner.invoke(app, ["chat"])
    assert out.exit_code == 2 and "hubzoid chat --hub ops" in out.output
