"""``hubzoid chat shares|collaborators`` (local mode).

The same service as ``/api/shares`` and ``/api/conversations/{id}/collaborators``,
acting as one account (``--user EMAIL``, default the local owner).
"""
from __future__ import annotations

from pathlib import Path

import typer

from ..control import cli_common as cc
from .engine_cli import HUB, JSON, USER, _local, _read_file

shares_app = typer.Typer(help="Snapshot links: preview, create, list, change, revoke, copy.", no_args_is_help=True)
collaborators_app = typer.Typer(help="People who can read or write a conversation.", no_args_is_help=True)


def register(chat_app: typer.Typer) -> None:
    chat_app.add_typer(shares_app, name="shares")
    chat_app.add_typer(collaborators_app, name="collaborators")


def _svc(loc):
    from .sharing import SharingService

    return SharingService(loc.hub, loc.engine.engine, loc.ctx.hub)


@shares_app.command("preview")
def sh_preview(hub: Path = HUB, file: Path = typer.Option(..., "--file", help="conversation_id, audience, allow_copy ..."),
               user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    body = _read_file(file, as_json)
    with loc.guard():
        conv, _ = loc.engine.readable(str(body.get("conversation_id") or ""), loc.actor_obj)
        cc.emit(_svc(loc).preview(conv, loc.actor_obj.id, body), as_json)


@shares_app.command("create")
def sh_create(hub: Path = HUB, preview: str = typer.Option(..., "--preview"), digest: str = typer.Option(..., "--digest"),
              user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).publish(loc.actor_obj.id, {"preview_id": preview, "digest": digest}), as_json)


@shares_app.command("list")
def sh_list(hub: Path = HUB, conversation: str = typer.Option(None, "--conversation"),
            all_hub: bool = typer.Option(False, "--all", help="Every link of this hub (no tokens)."),
            user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        items = _svc(loc).list_shares(owner_id=None if all_hub else loc.actor_obj.id, conversation_id=conversation)
    cc.emit({"items": items}, as_json)


@shares_app.command("revoke")
def sh_revoke(share_id: str, hub: Path = HUB, expected_revision: int = typer.Option(None, "--expected-revision"),
              user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        _svc(loc).revoke(share_id, loc.actor_obj.id, expected_revision, manager=True)
    cc.emit({"revoked": share_id}, as_json)


@shares_app.command("change")
def sh_change(share_id: str, hub: Path = HUB, file: Path = typer.Option(..., "--file"), user: str = USER,
              as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    body = _read_file(file, as_json)
    with loc.guard():
        cc.emit(_svc(loc).change(share_id, loc.actor_obj.id, body, manager=True), as_json)


@shares_app.command("copy")
def sh_copy(token: str, hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    """Copy a link you can read into your own conversation (previewed, then made)."""
    loc = _local(hub, as_json, user)
    with loc.guard():
        svc = _svc(loc)
        pv = svc.copy_preview(token, loc.actor_obj.id)
        cc.emit(svc.copy(token, loc.actor_obj.id, loc.actor_obj.email, loc.agent(), pv["digest"]), as_json)


@collaborators_app.command("list")
def co_list(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"), user: str = USER,
            as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        conv, role = loc.engine.readable(conversation, loc.actor_obj)
        cc.emit(_svc(loc).collaborators(conv, role), as_json)


@collaborators_app.command("set")
def co_set(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
           file: Path = typer.Option(..., "--file", help='{"members": [{"principal_id": "a@x", "role": "editor"}]}'),
           expected_revision: int = typer.Option(..., "--expected-revision"), user: str = USER,
           as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    body = _read_file(file, as_json)
    with loc.guard():
        conv, _ = loc.engine.readable(conversation, loc.actor_obj)
        cc.emit(_svc(loc).set_collaborators(conv, loc.actor_obj.id, body.get("members"), expected_revision), as_json)
