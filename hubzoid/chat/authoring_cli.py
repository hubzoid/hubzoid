"""``hubzoid workspace`` (local mode): hub files and their versions; prompts; OpenAPI preview.

Edits to a hub's own files go through ``hubzoid hub-changes``; the old
``workspace drafts`` commands only say so.

The same service as ``/api/chat/authoring`` and ``/api/chat/prompts``, acting as
one account (``--user EMAIL``, default the local owner). Editing the hub's files
directly with Claude Code or Codex stays as valid as ever.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from ..control import cli_common as cc
from .engine_cli import HUB, JSON, USER, _local

workspace_app = typer.Typer(help="Hub files and their versions, prompts, OpenAPI preview.", no_args_is_help=True)
drafts_app = typer.Typer(help="Removed: use `hubzoid hub-changes`.", no_args_is_help=True)
prompts_app = typer.Typer(help="Shared and personal prompts.", no_args_is_help=True)
workspace_app.add_typer(drafts_app, name="drafts", hidden=True)
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


# Workspace drafts were replaced by hub changes (c9b070b6): one reviewed path
# for edits to a hub's own files, and a file's history is its Git log. The
# commands stay, named, so an old script learns where they went instead of
# meeting "No such command".
DRAFTS_MOVED = ("Workspace drafts were replaced by hub changes. Propose an edit with "
                "`hubzoid hub-changes propose`, approve it with `hubzoid hub-changes decide`, and undo one with "
                "`hubzoid hub-changes undo`. A file's history is `hubzoid workspace versions <path>`.")
_ANY_ARGS = {"allow_extra_args": True, "ignore_unknown_options": True}


def _drafts_moved(name: str) -> None:
    @drafts_app.command(name, context_settings=_ANY_ARGS, help=f"Removed: {DRAFTS_MOVED}")
    def moved(ctx: typer.Context, as_json: bool = JSON) -> None:
        cc.fail(410, "moved", DRAFTS_MOVED, as_json, use="hubzoid hub-changes")


for _name in ("create", "update", "show", "submit", "review", "publish", "rollback"):
    _drafts_moved(_name)


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
