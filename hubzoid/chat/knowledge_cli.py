"""``hubzoid knowledge`` (local mode): collections, syncs, queries, citations, fetch.

The same service as ``/api/chat/retrieval``, acting as one account (``--user
EMAIL``, default the local owner). A collection's manifest is a YAML file
(``knowledge/collections/<id>.yaml`` by convention) applied with an expected
revision (``absent`` creates).
"""
from __future__ import annotations

import time
from pathlib import Path

import typer

from ..control import cli_common as cc
from .engine_cli import HUB, JSON, USER, _local

knowledge_app = typer.Typer(help="Your sources: collections, sync, query, citations, safe fetch.", no_args_is_help=True)
collections_app = typer.Typer(help="Collections of sources.", no_args_is_help=True)
jobs_app = typer.Typer(help="Syncs and their outcome.", no_args_is_help=True)
knowledge_app.add_typer(collections_app, name="collections")
knowledge_app.add_typer(jobs_app, name="jobs")


def _svc(loc):
    from .file_service import service_for as files_for
    from .retrieval import service_for

    return service_for(loc.hub, loc.engine.engine, loc.ctx.hub,
                       files_for(loc.hub, loc.engine.engine, loc.ctx.hub, loc.engine))


@collections_app.command("list")
def c_list(hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": _svc(loc).list(loc.actor_obj)}, as_json)


@collections_app.command("get")
def c_get(collection: str, hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).get(loc.actor_obj, collection), as_json)


@collections_app.command("apply")
def c_apply(file: Path = typer.Option(..., "--file", help="The collection manifest (YAML)."),
            expected_revision: str = typer.Option(..., "--expected-revision", help="A number, or 'absent' to create."),
            hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    from ..control import yamlfile

    loc = _local(hub, as_json, user)
    try:
        manifest = yamlfile.load(file).data
    except Exception as exc:  # noqa: BLE001 - the loader names the line
        cc.fail(422, "invalid_config", f"{file}: {exc}", as_json)
    with loc.guard():
        cc.emit(_svc(loc).apply(loc.actor_obj, manifest, expected_revision=expected_revision), as_json)


@collections_app.command("export")
def c_export(collection: str, hub: Path = typer.Option(Path("."), "--hub"), user: str = USER,
             as_json: bool = JSON) -> None:
    """Print the collection's manifest as YAML (or JSON with --json)."""
    import yaml

    loc = _local(hub, as_json, user)
    with loc.guard():
        m = _svc(loc).get(loc.actor_obj, collection)["manifest"]
    if as_json:
        cc.emit(m, True)
    else:
        typer.echo(yaml.safe_dump(m, sort_keys=False, allow_unicode=True))


@collections_app.command("delete")
def c_delete(collection: str, hub: Path = typer.Option(Path("."), "--hub"), user: str = USER,
             as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        _svc(loc).delete(loc.actor_obj, collection)
    cc.emit({"deleted": collection}, as_json)


@knowledge_app.command("sync")
def k_sync(collection: str, hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    """Extract, index and publish the collection's sources (waits for the result)."""
    loc = _local(hub, as_json, user)
    with loc.guard():
        job = _svc(loc).start_sync(loc.actor_obj, collection, background=False)
    cc.emit(job, as_json)
    if job["state"] != "ready":
        raise typer.Exit(1)


@jobs_app.command("get")
def j_get(job: str, hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).job(loc.actor_obj, job), as_json)


@jobs_app.command("wait")
def j_wait(job: str, hub: Path = typer.Option(Path("."), "--hub"), timeout: float = 300, user: str = USER,
           as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    end = time.time() + timeout
    with loc.guard():
        while True:
            out = _svc(loc).job(loc.actor_obj, job)
            if out["state"] in ("ready", "failed", "cancelled") or time.time() > end:
                break
            time.sleep(0.5)
    cc.emit(out, as_json)
    if out["state"] not in ("ready", "failed", "cancelled"):
        raise typer.Exit(cc.EXIT_PENDING)
    if out["state"] != "ready":
        raise typer.Exit(1)


@knowledge_app.command("query")
def k_query(text: str, hub: Path = typer.Option(Path("."), "--hub"),
            collection: list[str] = typer.Option(None, "--collection"), top_k: int = typer.Option(None, "--top-k"),
            user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).query(loc.actor_obj, text, collection_ids=collection or None, top_k=top_k), as_json)


@knowledge_app.command("citation")
def k_citation(citation: str, hub: Path = typer.Option(Path("."), "--hub"), user: str = USER,
               as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).citation(loc.actor_obj, citation), as_json)


@knowledge_app.command("fetch")
def k_fetch(url: str, hub: Path = typer.Option(Path("."), "--hub"), as_json: bool = JSON) -> None:
    """Fetch one public https page with the safe loader and show its text."""
    from .retrieval import extract

    loc = _local(hub, as_json, None)
    svc = _svc(loc)
    web = svc.settings()["web"]
    with loc.guard():
        final, mime, data = svc.fetch(url, web["limits"], allowed=web["allowed_domains"], blocked=web["blocked_domains"])
        title, pages = extract(final, mime, data)
    cc.emit({"url": final, "title": title, "mime_type": mime, "bytes": len(data),
             "excerpt": "\n".join(t for _p, t in pages)[:2000]}, as_json)
