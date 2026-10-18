"""``hubzoid chat organization|folders|tags|bookmarks|organize|search|exports|imports`` (local mode)."""
from __future__ import annotations

import uuid
from pathlib import Path

import typer

from ..control import cli_common as cc
from .engine_cli import HUB, JSON, USER, _local, _read_file

organization_app = typer.Typer(help="Your organisation: export, apply a manifest, bulk changes.", no_args_is_help=True)
folders_app = typer.Typer(help="Personal folders.", no_args_is_help=True)
tags_app = typer.Typer(help="Personal tags.", no_args_is_help=True)
bookmarks_app = typer.Typer(help="Bookmarks.", no_args_is_help=True)
search_app = typer.Typer(help="Full-text search.", no_args_is_help=True)
exports_app = typer.Typer(help="Export conversations as a hubzoid-exchange package.", no_args_is_help=True)
imports_app = typer.Typer(help="Preview and apply an import.", no_args_is_help=True)


def register(chat_app: typer.Typer) -> None:
    for name, sub in (("organization", organization_app), ("folders", folders_app), ("tags", tags_app),
                      ("bookmarks", bookmarks_app), ("search", search_app), ("exports", exports_app),
                      ("imports", imports_app)):
        chat_app.add_typer(sub, name=name)
    chat_app.command("organize")(organize)


def _svc(loc):
    from .organization import OrganizationService

    return OrganizationService(loc.engine.engine, loc.ctx.hub, loc.hub)


def _readable(loc):
    return lambda cid: loc.engine.readable(cid, loc.actor_obj)


@organization_app.command("export")
def org_export(hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).export(loc.actor_obj.id), as_json)


@organization_app.command("apply")
def org_apply(hub: Path = HUB, file: Path = typer.Option(..., "--file"), digest: str = typer.Option(None, "--digest"),
              apply: bool = typer.Option(False, "--apply", help="Apply the dry run with this --digest."),
              user: str = USER, as_json: bool = JSON) -> None:
    import yaml

    loc = _local(hub, as_json, user)
    manifest = yaml.safe_load(Path(file).read_text("utf-8"))
    with loc.guard():
        cc.emit(_svc(loc).apply_manifest(loc.actor_obj.id, manifest, dry_run=not apply, digest=digest,
                                         readable=_readable(loc)), as_json)


@organization_app.command("bulk")
def org_bulk(hub: Path = HUB, file: Path = typer.Option(..., "--file"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).bulk(loc.actor_obj.id, _read_file(file, as_json), readable=_readable(loc)), as_json)


def organize(conversation_id: str, hub: Path = HUB, file: Path = typer.Option(..., "--file"), user: str = USER,
             as_json: bool = JSON) -> None:
    """Folder, pin, archive and tags of one conversation (yours only)."""
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).organize(loc.actor_obj.id, conversation_id, _read_file(file, as_json),
                                   readable=_readable(loc)), as_json)


@folders_app.command("list")
def f_list(hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": _svc(loc).export(loc.actor_obj.id)["folders"]}, as_json)


@folders_app.command("create")
def f_create(hub: Path = HUB, file: Path = typer.Option(..., "--file"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).create_folder(loc.actor_obj.id, _read_file(file, as_json)), as_json)


@folders_app.command("update")
def f_update(folder_id: str, hub: Path = HUB, file: Path = typer.Option(..., "--file"), user: str = USER,
             as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).update_folder(loc.actor_obj.id, folder_id, _read_file(file, as_json)), as_json)


@folders_app.command("delete")
def f_delete(folder_id: str, hub: Path = HUB, expected_revision: int = typer.Option(..., "--expected-revision"),
             user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        _svc(loc).delete_folder(loc.actor_obj.id, folder_id, {"expected_revision": expected_revision})
    cc.emit({"deleted": folder_id}, as_json)


@folders_app.command("context")
def f_context(folder_id: str, hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).resolve_context(loc.actor_obj.id, folder_id), as_json)


@tags_app.command("list")
def t_list(hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": _svc(loc).export(loc.actor_obj.id)["tags"]}, as_json)


@tags_app.command("create")
def t_create(hub: Path = HUB, name: str = typer.Option(..., "--name"), color: str = typer.Option("neutral", "--color"),
             user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).create_tag(loc.actor_obj.id, {"name": name, "color": color}), as_json)


@tags_app.command("delete")
def t_delete(tag_id: str, hub: Path = HUB, expected_revision: int = typer.Option(..., "--expected-revision"),
             user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        _svc(loc).delete_tag(loc.actor_obj.id, tag_id, expected_revision)
    cc.emit({"deleted": tag_id}, as_json)


@bookmarks_app.command("list")
def b_list(hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": _svc(loc).export(loc.actor_obj.id)["bookmarks"]}, as_json)


@bookmarks_app.command("create")
def b_create(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"),
             message: str = typer.Option(None, "--message"), label: str = typer.Option("Bookmark", "--label"),
             user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).add_bookmark(loc.actor_obj.id, {"conversation_id": conversation, "message_id": message,
                                                          "label": label}, readable=_readable(loc)), as_json)


@bookmarks_app.command("delete")
def b_delete(bookmark_id: str, hub: Path = HUB, expected_revision: int = typer.Option(..., "--expected-revision"),
             user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        _svc(loc).delete_bookmark(loc.actor_obj.id, bookmark_id, expected_revision)
    cc.emit({"deleted": bookmark_id}, as_json)


@search_app.command("query")
def s_query(query: str, hub: Path = HUB, types: str = typer.Option(None, "--types"),
            limit: int = typer.Option(None, "--limit"), cursor: str = typer.Option(None, "--cursor"),
            archived: str = typer.Option("exclude", "--archived"), user: str = USER, as_json: bool = JSON) -> None:
    from .search import SearchService

    loc = _local(hub, as_json, user)
    with loc.guard():
        svc = SearchService(loc.engine.engine, loc.ctx.hub, loc.hub)
        cc.emit(svc.search(loc.actor_obj, query, types=types.split(",") if types else None, limit=limit,
                           cursor=cursor, archived=archived), as_json)


@search_app.command("status")
def s_status(hub: Path = HUB, as_json: bool = JSON) -> None:
    from .search import SearchService

    loc = _local(hub, as_json, None)
    cc.emit(SearchService(loc.engine.engine, loc.ctx.hub, loc.hub).status(), as_json)


@search_app.command("rebuild")
def s_rebuild(hub: Path = HUB, as_json: bool = JSON) -> None:
    from .search import SearchService

    loc = _local(hub, as_json, None)
    cc.emit(SearchService(loc.engine.engine, loc.ctx.hub, loc.hub).rebuild(), as_json)


@exports_app.command("create")
def e_create(hub: Path = HUB, conversation: list[str] = typer.Option(..., "--conversation"),
             output: Path = typer.Option(..., "--output"), user: str = USER, as_json: bool = JSON) -> None:
    from . import portability as port

    loc = _local(hub, as_json, user)
    with loc.guard():
        data = port.export_package(loc.engine.engine, loc.hub, actor_id=loc.actor_obj.id, conversation_ids=conversation,
                                   readable=_readable(loc), organization=_svc(loc).export(loc.actor_obj.id))
    output.write_bytes(data)
    cc.emit({"saved_to": str(output), "bytes": len(data)}, as_json)


@imports_app.command("preview")
def i_preview(hub: Path = HUB, file: Path = typer.Option(..., "--file"), user: str = USER, as_json: bool = JSON) -> None:
    from . import portability as port

    loc = _local(hub, as_json, user)
    data = Path(file).read_bytes()
    with loc.guard():
        fmt, records, blobs, problems = port.parse(data)
        objects = port.preview(records, problems, blobs)
        meta = port.ImportStore(loc.hub).save(loc.actor_obj.id, data, fmt, objects)
    cc.emit({"id": meta["id"], "digest": port.digest_with(meta, {}), "format": fmt, "objects": objects}, as_json)


@imports_app.command("apply")
def i_apply(hub: Path = HUB, preview: str = typer.Option(..., "--preview"), digest: str = typer.Option(..., "--digest"),
            dispositions_file: Path = typer.Option(None, "--dispositions-file"),
            apply: bool = typer.Option(False, "--apply"), user: str = USER, as_json: bool = JSON) -> None:
    from . import portability as port

    if not apply:
        cc.fail(400, "invalid_request", "Add --apply to import (run 'imports preview' first).", as_json)
    loc = _local(hub, as_json, user)
    dispositions = _read_file(dispositions_file, as_json) if dispositions_file else {}
    store = port.ImportStore(loc.hub)
    with loc.guard():
        meta, data = store.load(preview, loc.actor_obj.id)
        if port.digest_with(meta, dispositions) != digest:
            cc.fail(409, "revision_conflict", "Apply exactly the preview and choices you reviewed.", as_json)
        if "result" in meta:
            cc.emit(meta["result"], as_json)
            return
        proposed = {f"{o['type']}:{o['source_id']}": o["disposition"] for o in meta["objects"]}
        for k, v in dispositions.items():
            if proposed.get(k) == "reject" and v == "import":
                cc.fail(422, "validation_failed", f"{k} is corrupt or incomplete and can't be imported.", as_json)
        _fmt, records, blobs, _p = port.parse(data)
        result = port.apply(loc.engine.engine, loc.hub, loc.ctx.hub, actor_id=loc.actor_obj.id,
                            actor_email=loc.actor_obj.email, agent=loc.agent(), records=records, blobs=blobs,
                            dispositions={**proposed, **dispositions}, operation_id=str(uuid.uuid4()))
        store.done(meta["id"], result)
    cc.emit(result, as_json)
