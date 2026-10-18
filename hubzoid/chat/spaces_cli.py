"""``hubzoid spaces|notes|calendar`` (local mode): the same service as the API, as one account."""
from __future__ import annotations

import time
from pathlib import Path

import typer

from ..control import cli_common as cc
from .engine_cli import HUB, JSON, USER, _local

spaces_app = typer.Typer(help="Channels and direct messages.", no_args_is_help=True)
notes_app = typer.Typer(help="Personal and channel notes.", no_args_is_help=True)
calendar_app = typer.Typer(help="Events and reminders.", no_args_is_help=True)
OPT_HUB = typer.Option(Path("."), "--hub")


def _svc(loc):
    from .spaces import service_for

    return service_for(loc.hub, loc.engine.engine, loc.ctx.hub)


@spaces_app.command("list")
def s_list(hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": _svc(loc).list_spaces(loc.actor_obj)}, as_json)


@spaces_app.command("create")
def s_create(name: str, hub: Path = OPT_HUB, person: list[str] = typer.Option(None, "--person"),
             open_: bool = typer.Option(False, "--open"), dm: bool = typer.Option(False, "--dm"),
             user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).create_space(loc.actor_obj, name=name, kind="dm" if dm else "channel", people=person or [],
                                       open_=open_), as_json)


@spaces_app.command("posts")
def s_posts(space: str, hub: Path = OPT_HUB, thread: str = typer.Option(None, "--thread"), user: str = USER,
            as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).posts(loc.actor_obj, space, parent_id=thread), as_json)


@spaces_app.command("post")
def s_post(space: str, body: str, hub: Path = OPT_HUB, thread: str = typer.Option(None, "--thread"), user: str = USER,
           as_json: bool = JSON) -> None:
    """Post (a mention of @agent is answered in the web app, where the agent runs)."""
    loc = _local(hub, as_json, user)
    with loc.guard():
        post, _agent = _svc(loc).post(loc.actor_obj, space, body, parent_id=thread)
    cc.emit(post, as_json)


@notes_app.command("list")
def n_list(hub: Path = HUB, space: str = typer.Option(None, "--space"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": _svc(loc).list_notes(loc.actor_obj, space)}, as_json)


@notes_app.command("create")
def n_create(title: str, hub: Path = OPT_HUB, file: Path = typer.Option(None, "--file"),
             space: str = typer.Option(None, "--space"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).create_note(loc.actor_obj, title, file.read_text() if file else "", space), as_json)


@notes_app.command("save")
def n_save(note: str, file: Path = typer.Option(..., "--file"), expected_revision: int = typer.Option(..., "--expected-revision"),
           title: str = typer.Option(None, "--title"), hub: Path = OPT_HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).save_note(loc.actor_obj, note, title=title or "", body=file.read_text(),
                                    expected_revision=expected_revision), as_json)


@calendar_app.command("list")
def c_list(hub: Path = HUB, days: int = typer.Option(14, "--days"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    now = time.time()
    with loc.guard():
        cc.emit({"items": _svc(loc).list_events(loc.actor_obj, now - 86400, now + days * 86400)}, as_json)


@calendar_app.command("create")
def c_create(title: str, start: float = typer.Option(..., "--start", help="Seconds since 1970 (UTC)."),
             minutes: int = typer.Option(60, "--minutes"), timezone: str = typer.Option("UTC", "--timezone"),
             repeat: str = typer.Option("none", "--repeat"), reminder: int = typer.Option(None, "--reminder-minutes"),
             attendee: list[str] = typer.Option(None, "--attendee"), space: str = typer.Option(None, "--space"),
             hub: Path = OPT_HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).save_event(loc.actor_obj, {
            "title": title, "starts_at": start, "ends_at": start + minutes * 60, "timezone": timezone, "repeat": repeat,
            "reminder_minutes": reminder, "attendees": attendee or [], "space_id": space}), as_json)


@calendar_app.command("delete")
def c_delete(event: str, hub: Path = OPT_HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        _svc(loc).delete_event(loc.actor_obj, event)
    cc.emit({"deleted": event}, as_json)
