"""``hubzoid chat shortcuts|devices`` (local mode, as one account with ``--user``)."""
from __future__ import annotations

from pathlib import Path

import typer

from ..control import cli_common as cc
from . import devices
from .engine_cli import HUB, JSON, USER, _local

shortcuts_app = typer.Typer(help="Keyboard shortcuts: list, change, turn off, reset, check.", no_args_is_help=True)
devices_app = typer.Typer(help="Install metadata, doctor and the tested device matrix.", no_args_is_help=True)


def register(chat_app: typer.Typer) -> None:
    chat_app.add_typer(shortcuts_app, name="shortcuts")
    chat_app.add_typer(devices_app, name="devices")


def _cfg(hub: Path):
    from .config import service_for

    return service_for(Path(hub))


def _mine(loc) -> tuple[int, dict]:
    prefs = _cfg(loc.hub).get_preferences(loc.actor_obj.id)
    return prefs["revision"], prefs["values"]


def _save(loc, values: dict, as_json: bool) -> None:
    from .config import ConfigError

    rev, current = _mine(loc)
    try:
        out = _cfg(loc.hub).set_preferences(loc.actor_obj.id, {**current, **values}, expected_revision=rev)
    except ConfigError as exc:
        cc.fail(exc.status, exc.code, exc.message, as_json, **exc.extra)
    cc.emit(devices.effective_shortcuts(_cfg(loc.hub).active().document, out["values"]), as_json)


@shortcuts_app.command("list")
def sc_list(hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    cc.emit(devices.effective_shortcuts(_cfg(loc.hub).active().document, _mine(loc)[1]), as_json)


@shortcuts_app.command("set")
def sc_set(action: str, chord: str, hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    _save(loc, {"shortcuts": {**(_mine(loc)[1].get("shortcuts") or {}), action: chord}}, as_json)


@shortcuts_app.command("disable")
def sc_disable(action: str, hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    _save(loc, {"shortcuts": {**(_mine(loc)[1].get("shortcuts") or {}), action: None}}, as_json)


@shortcuts_app.command("reset")
def sc_reset(action: str = typer.Argument(None), hub: Path = HUB, all_: bool = typer.Option(False, "--all"),
             user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    mine = dict(_mine(loc)[1].get("shortcuts") or {})
    if all_:
        mine = {}
    elif action:
        mine.pop(action, None)
    else:
        cc.fail(400, "invalid_request", "Name an action or pass --all.", as_json)
    _save(loc, {"shortcuts": mine}, as_json)


@shortcuts_app.command("check")
def sc_check(hub: Path = HUB, file: Path = typer.Option(None, "--file"), as_json: bool = JSON) -> None:
    """Check the deployment's shortcut defaults in chat/config.yaml."""
    from . import config as cfg

    path = file or _cfg(hub).file_path()
    try:
        doc, _ = cfg.parse_file(Path(path))
        devices.check_shortcuts(doc["devices"]["shortcuts"]["defaults"],
                                single_key=doc["devices"]["shortcuts"]["single_key"])
        merged = devices.effective_shortcuts(doc, {})
    except cfg.ConfigError as exc:
        cc.fail(exc.status, exc.code, exc.message, as_json, **exc.extra)
    except devices.Err as exc:
        cc.fail(exc.status, exc.code, exc.message, as_json, **exc.extra)
    if merged["conflicts"]:
        chord, actions = next(iter(merged["conflicts"].items()))
        cc.fail(422, "shortcut_conflict", f"{chord} is used by {' and '.join(actions)}.", as_json)
    cc.emit({"valid": True, "actions": merged["actions"]}, as_json)


@devices_app.command("manifest")
def dv_manifest(hub: Path = HUB, as_json: bool = JSON) -> None:
    doc = _cfg(hub).active().document["devices"]
    cc.emit({"install": doc["install"], "share_target": doc["install"]["share_target"] and doc["offline"]["shell_cache"],
             "served_at": "/manifest.webmanifest"}, as_json)


@devices_app.command("doctor")
def dv_doctor(hub: Path = HUB, url: str = typer.Option(None, "--url"), as_json: bool = JSON) -> None:
    from .. import appmode

    cc.emit({"checks": devices.doctor(_cfg(hub).active().document, public_url=url or appmode.public_url() or "")},
            as_json)


@devices_app.command("support")
def dv_support(as_json: bool = JSON) -> None:
    cc.emit(devices.SUPPORT, as_json)
