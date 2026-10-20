"""``hubzoid chat feedback|helpers|title|notifications`` (local mode).

The same service as the ``/api/chat/feedback``, ``/helpers``,
``/notifications`` and ``/notification-targets`` routes, acting as one
account (``--user EMAIL``, default the local owner).
"""
from __future__ import annotations

from pathlib import Path

import typer

from ..control import cli_common as cc
from .engine_cli import HUB, JSON, USER, _local, _read_file

feedback_app = typer.Typer(help="Ratings of answers.", no_args_is_help=True)
helpers_app = typer.Typer(help="Generated follow-ups and titles.", no_args_is_help=True)
title_app = typer.Typer(help="Conversation titles.", no_args_is_help=True)
notifications_app = typer.Typer(help="Your inbox and notification targets.", no_args_is_help=True)
targets_app = typer.Typer(help="External notification targets.", no_args_is_help=True)
notifications_app.add_typer(targets_app, name="targets")


def register(chat_app: typer.Typer) -> None:
    for name, sub in (("feedback", feedback_app), ("helpers", helpers_app), ("title", title_app),
                      ("notifications", notifications_app)):
        chat_app.add_typer(sub, name=name)


def _svc(loc):
    from .feedback import service_for

    return service_for(loc.hub, loc.engine.engine, loc.ctx.hub)


@feedback_app.command("put")
def fb_put(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
           message: str = typer.Option(..., "--message"), rating: str = typer.Option(..., "--rating"),
           reason: list[str] = typer.Option([], "--reason"), comment: str = typer.Option(None, "--comment"),
           score: int = typer.Option(None, "--score"), tag: list[str] = typer.Option([], "--tag"),
           expected_revision: str = typer.Option("absent", "--expected-revision"), user: str = USER,
           as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    expected = expected_revision if expected_revision == "absent" else int(expected_revision)
    with loc.guard():
        conv, _ = loc.engine.readable(conversation, loc.actor_obj)
        cc.emit(_svc(loc).put_feedback(conv, loc.actor_obj.id, message, {
            "rating": rating, "reason_ids": reason, "comment": comment, "score": score, "tags": tag,
            "expected_revision": expected}, source="cli"), as_json)


@feedback_app.command("remove")
def fb_remove(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
              message: str = typer.Option(..., "--message"),
              expected_revision: int = typer.Option(None, "--expected-revision"), user: str = USER,
              as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        loc.engine.readable(conversation, loc.actor_obj)
        _svc(loc).delete_feedback(loc.actor_obj.id, message, expected_revision)
    cc.emit({"deleted": True}, as_json)


@feedback_app.command("list")
def fb_list(hub: Path = HUB, agent: str = typer.Option(None, "--agent"), rating: str = typer.Option(None, "--rating"),
            source: str = typer.Option(None, "--source", help="web, tool, slack, whatsapp or cli"),
            reason: str = typer.Option(None, "--reason"), limit: int = 50, as_json: bool = JSON) -> None:
    """Every rating of this hub, from every surface (the CLI runs as someone who controls the hub folder)."""
    loc = _local(hub, as_json, None)
    with loc.guard():
        cc.emit(_svc(loc).list_feedback(agent_id=agent, rating=rating, source=source, reason=reason, limit=limit),
                as_json)


@feedback_app.command("export")
def fb_export(hub: Path = HUB, fmt: str = typer.Option("json", "--format"),
              output: Path = typer.Option(..., "--output"), as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, None)
    if fmt not in ("json", "csv"):
        cc.fail(422, "validation_failed", "format is json or csv.", as_json)
    with loc.guard():
        data, _media = _svc(loc).export_feedback(fmt)
    output.write_bytes(data)
    cc.emit({"saved_to": str(output), "bytes": len(data)}, as_json)


@helpers_app.command("create")
def hl_create(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
              kind: str = typer.Option(..., "--kind"), branch: str = typer.Option(..., "--branch"),
              input_revision: int = typer.Option(..., "--input-revision"), user: str = USER,
              as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        conv, _ = loc.engine.readable(conversation, loc.actor_obj)
        job = _svc(loc).start_helper(conv, loc.actor_obj.id, kind, branch, input_revision)
        if job["state"] == "queued":
            job = _svc(loc).run_helper(job["id"])
    cc.emit(job, as_json)


@helpers_app.command("list")
def hl_list(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
            branch: str = typer.Option(None, "--branch"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        loc.engine.readable(conversation, loc.actor_obj)
        cc.emit({"items": _svc(loc).list_helpers(conversation, branch, loc.actor_obj.id)}, as_json)


@title_app.command("set")
def title_set(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
              title: str = typer.Option(..., "--title"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    clean = " ".join(title.split())
    if not clean or len(clean) > 200:
        cc.fail(422, "validation_failed", "A title is 1 to 200 characters.", as_json)
    with loc.guard():
        _, role = loc.engine.readable(conversation, loc.actor_obj)
        if role != "owner":
            cc.fail(403, "permission_denied", "Only the owner can rename this conversation.", as_json)
        loc.ctx.store.update_conversation(conversation, title=clean, title_source="user")
    cc.emit({"id": conversation, "title": clean, "source": "user"}, as_json)


@notifications_app.command("list")
def nt_list(hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).notifications(loc.actor_obj.id), as_json)


def _mark(state: str):
    def cmd(item_id: str, hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
        loc = _local(hub, as_json, user)
        with loc.guard():
            cc.emit(_svc(loc).mark(loc.actor_obj.id, item_id, state), as_json)

    cmd.__doc__ = f"Mark a notification {state}."
    return cmd


notifications_app.command("read")(_mark("read"))
notifications_app.command("dismiss")(_mark("dismissed"))


def _scope(loc, scope: str) -> str:
    # Local mode acts for someone who controls the hub folder, so hub scope is allowed.
    return _svc(loc).scope_of(scope, loc.actor_obj.id, manager=True)


@targets_app.command("get")
def tg_get(hub: Path = HUB, scope: str = typer.Option("own", "--scope"), user: str = USER,
           as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).list_targets(_scope(loc, scope)), as_json)


@targets_app.command("put")
def tg_put(target_id: str, hub: Path = HUB, scope: str = typer.Option("own", "--scope"),
           file: Path = typer.Option(..., "--file", help="The target as JSON."),
           expected_revision: str = typer.Option("absent", "--expected-revision"), user: str = USER,
           as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    body = _read_file(file, as_json)
    expected = expected_revision if expected_revision == "absent" else int(expected_revision)
    with loc.guard():
        key = _scope(loc, scope)
        cc.emit(_svc(loc).put_target(key, target_id, body, owner_id=loc.actor_obj.id if scope == "own" else None,
                                     expected_revision=expected), as_json)


@targets_app.command("remove")
def tg_remove(target_id: str, hub: Path = HUB, scope: str = typer.Option("own", "--scope"),
              expected_revision: int = typer.Option(..., "--expected-revision"), user: str = USER,
              as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        _svc(loc).delete_target(_scope(loc, scope), target_id, expected_revision)
    cc.emit({"deleted": target_id}, as_json)


@targets_app.command("verify")
def tg_verify(target_id: str, hub: Path = HUB, scope: str = typer.Option("own", "--scope"), user: str = USER,
              as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).verify_target(_scope(loc, scope), target_id), as_json)


@notifications_app.command("deliveries")
def nt_deliveries(hub: Path = HUB, target: str = typer.Option(..., "--target"),
                  scope: str = typer.Option("own", "--scope"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": _svc(loc).delivery_history(_scope(loc, scope), target)}, as_json)

