"""``hubzoid models`` (local mode): the catalog people may pick from, and presets."""
from __future__ import annotations

from pathlib import Path

import typer

from ..control import cli_common as cc
from .engine_cli import HUB, JSON, USER, _local

models_app = typer.Typer(help="Models people may pick, and presets.", no_args_is_help=True)
presets_app = typer.Typer(help="Your presets and the hub's shared ones.", no_args_is_help=True)
models_app.add_typer(presets_app, name="presets")


def _svc(loc):
    loc.agent()  # the runtime decides what it offers when no catalog is configured
    return loc.ctx.models


@models_app.command("catalog")
def m_catalog(hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).describe(loc.actor_obj.email), as_json)


@presets_app.command("list")
def p_list(hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": _svc(loc).list_presets(loc.actor_obj)}, as_json)


@presets_app.command("put")
def p_put(file: Path = typer.Option(..., "--file", help="The preset (YAML)."),
          expected_revision: str = typer.Option(..., "--expected-revision", help="A number, or 'absent' to create."),
          hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    from ..control import yamlfile

    loc = _local(hub, as_json, user)
    try:
        body = yamlfile.load(file).data
    except Exception as exc:  # noqa: BLE001
        cc.fail(422, "invalid_config", f"{file}: {exc}", as_json)
    with loc.guard():
        cc.emit(_svc(loc).put_preset(loc.actor_obj, str(body.get("id") or ""), body, expected_revision), as_json)


@presets_app.command("delete")
def p_delete(preset: str, hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        _svc(loc).delete_preset(loc.actor_obj, preset)
    cc.emit({"deleted": preset}, as_json)
