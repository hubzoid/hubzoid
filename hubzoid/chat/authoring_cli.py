"""``hubzoid workspace`` (local mode): draft, review and publish hub files; prompts; OpenAPI preview.

The same service as ``/api/chat/authoring`` and ``/api/chat/prompts``, acting as
one account (``--user EMAIL``, default the local owner). Editing the hub's files
directly with Claude Code or Codex stays as valid as ever; this is the reviewed
path the Workspace uses.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from ..control import cli_common as cc
from .engine_cli import HUB, JSON, USER, _local

workspace_app = typer.Typer(help="Draft, review and publish agent, skill and prompt files.", no_args_is_help=True)
drafts_app = typer.Typer(help="Drafts of hub files.", no_args_is_help=True)
prompts_app = typer.Typer(help="Shared and personal prompts.", no_args_is_help=True)
workspace_app.add_typer(drafts_app, name="drafts")
workspace_app.add_typer(prompts_app, name="prompts")


def _svc(loc):
    from .authoring import service_for

    return service_for(loc.hub, loc.engine.engine, loc.ctx.hub)


@workspace_app.command("files")
def w_files(hub: Path = HUB, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, None)
    with loc.guard():
        cc.emit({"items": _svc(loc).files()}, as_json)


@workspace_app.command("versions")
def w_versions(path: str, hub: Path = typer.Option(Path("."), "--hub"), as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, None)
    with loc.guard():
        cc.emit({"items": _svc(loc).versions(path)}, as_json)


@drafts_app.command("create")
def d_create(path: str, hub: Path = typer.Option(Path("."), "--hub"),
             content_file: Path = typer.Option(None, "--content-file", help="The new content (default: the current file)."),
             user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).create_draft(loc.actor_obj, path, content_file.read_text() if content_file else None), as_json)


@drafts_app.command("update")
def d_update(draft: str, content_file: Path = typer.Option(..., "--content-file"),
             expected_revision: int = typer.Option(..., "--expected-revision"),
             hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).update_draft(loc.actor_obj, draft, content_file.read_text(), expected_revision), as_json)


@drafts_app.command("show")
def d_show(draft: str, hub: Path = typer.Option(Path("."), "--hub"), as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, None)
    with loc.guard():
        out = _svc(loc).get_draft(draft)
    if as_json:
        cc.emit(out, True)
    else:
        typer.echo(out["diff"] or "(no changes)")
        for p in out["problems"]:
            typer.echo(f"problem: {p['message']}")


@drafts_app.command("submit")
def d_submit(draft: str, expected_revision: int = typer.Option(..., "--expected-revision"),
             hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).submit(loc.actor_obj, draft, expected_revision), as_json)


@drafts_app.command("review")
def d_review(draft: str, decision: str = typer.Option(..., "--decision", help="approve or reject"),
             digest: str = typer.Option(..., "--digest"), note: str = typer.Option(None, "--note"),
             hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).review(loc.actor_obj, draft, decision=decision, digest=digest, note=note), as_json)


@drafts_app.command("publish")
def d_publish(draft: str, digest: str = typer.Option(..., "--digest"), hub: Path = typer.Option(Path("."), "--hub"),
              user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).publish(loc.actor_obj, draft, digest), as_json)


@drafts_app.command("rollback")
def d_rollback(path: str, version: str = typer.Option(..., "--version"), hub: Path = typer.Option(Path("."), "--hub"),
               user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).rollback_draft(loc.actor_obj, path, version), as_json)


@workspace_app.command("openapi-preview")
def w_openapi(file: Path = typer.Option(..., "--file", help="An OpenAPI document (JSON or YAML)."),
              select: list[str] = typer.Option(None, "--select", help="Operation ids to turn into tools."),
              hub: Path = HUB, as_json: bool = JSON) -> None:
    import yaml

    loc = _local(hub, as_json, None)
    text = file.read_text(encoding="utf-8")
    spec = json.loads(text) if file.suffix == ".json" else yaml.safe_load(text)
    with loc.guard():
        cc.emit(_svc(loc).openapi_preview(spec, select or None), as_json)


@prompts_app.command("list")
def p_list(hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": _svc(loc).prompts(loc.actor_obj)}, as_json)


@prompts_app.command("render")
def p_render(prompt: str, hub: Path = typer.Option(Path("."), "--hub"), scope: str = typer.Option("hub", "--scope"),
             var: list[str] = typer.Option(None, "--var", help="name=value"), user: str = USER,
             as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    values = dict(v.split("=", 1) for v in (var or []) if "=" in v)
    with loc.guard():
        out = _svc(loc).render(loc.actor_obj, prompt, scope, values)
    cc.emit(out, as_json) if as_json else typer.echo(out["text"])
