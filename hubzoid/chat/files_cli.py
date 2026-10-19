"""``hubzoid files`` and ``hubzoid artifacts`` (local mode).

The same service as ``/api/chat/files`` and ``/api/chat/artifacts``, acting as
one account (``--user EMAIL``, default the local owner). ``files upload`` sends
the file in resumable chunks exactly as the API does, then waits for the type
check unless ``--no-wait``.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path

import typer

from ..control import cli_common as cc
from .engine_cli import HUB, JSON, USER, _local

files_app = typer.Typer(help="Files with lasting identity: upload, reuse, download, delete.", no_args_is_help=True)
artifacts_app = typer.Typer(help="Documents made in conversations and their versions.", no_args_is_help=True)


def _svc(loc):
    from .file_service import service_for

    return service_for(loc.hub, loc.engine.engine, loc.ctx.hub, loc.engine)


@files_app.command("upload")
def f_upload(path: Path = typer.Argument(..., help="The file to upload."), hub: Path = typer.Option(Path("."), "--hub"),
             conversation: str = typer.Option(None, "--conversation", help="Attach it to this conversation."),
             mime: str = typer.Option(None, "--mime-type"), no_wait: bool = typer.Option(False, "--no-wait"),
             key: str = typer.Option(None, "--idempotency-key"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    if not path.is_file():
        cc.fail(400, "invalid_request", f"{path} is not a file.", as_json)
    data = path.read_bytes()
    svc = _svc(loc)
    with loc.guard():
        up = svc.start_upload(loc.actor_obj, {"name": path.name, "size_bytes": len(data), "mime_type": mime or "",
                                              "conversation_id": conversation,
                                              "content_sha256": hashlib.sha256(data).hexdigest()},
                              key=key or str(uuid.uuid4()))
        offset = up["offset"]
        while offset < len(data):
            chunk = data[offset:offset + up["chunk_bytes"]]
            offset = svc.put_chunk(loc.actor_obj, up["upload_id"], offset, chunk,
                                   hashlib.sha256(chunk).hexdigest())["offset"]
        desc = svc.complete(loc.actor_obj, up["upload_id"], hashlib.sha256(data).hexdigest())
        if conversation:
            desc = {**desc, "attached": svc.attach(loc.actor_obj, desc["file_id"], conversation)} \
                if _wait(svc, loc, desc["file_id"], 30)["state"] == "ready" else desc
        elif not no_wait:
            desc = _wait(svc, loc, desc["file_id"], 30)
    cc.emit(desc, as_json)


def _wait(svc, loc, file_id: str, timeout: float) -> dict:
    end = time.time() + timeout
    while True:
        desc = svc.get(loc.actor_obj, file_id)
        if desc["state"] != "validating" or time.time() > end:
            return desc
        time.sleep(0.2)


@files_app.command("wait")
def f_wait(file_id: str, hub: Path = typer.Option(Path("."), "--hub"), timeout: float = 120, user: str = USER,
           as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        desc = _wait(_svc(loc), loc, file_id, timeout)
    cc.emit(desc, as_json)
    if desc["state"] == "validating":
        raise typer.Exit(cc.EXIT_PENDING)
    if desc["state"] != "ready":
        raise typer.Exit(1)


@files_app.command("status")
def f_status(file_id: str, hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).get(loc.actor_obj, file_id), as_json)


@files_app.command("list")
def f_list(hub: Path = HUB, conversation: str = typer.Option(None, "--conversation"), user: str = USER,
           as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).list(loc.actor_obj, conversation_id=conversation), as_json)


@files_app.command("preview")
def f_preview(file_id: str, hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).preview(loc.actor_obj, file_id), as_json)


@files_app.command("download")
def f_download(file_id: str, output: Path = typer.Option(..., "--output"), hub: Path = typer.Option(Path("."), "--hub"),
               user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        desc, data = _svc(loc).content(loc.actor_obj, file_id)
    output.write_bytes(data)
    cc.emit({"saved_to": str(output), "bytes": len(data), "content_sha256": desc["content_sha256"]}, as_json)


@files_app.command("attach")
def f_attach(file_id: str, conversation: str = typer.Option(..., "--conversation"),
             hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).attach(loc.actor_obj, file_id, conversation), as_json)


@files_app.command("references")
def f_refs(file_id: str, hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": _svc(loc).references_of(loc.actor_obj, file_id)}, as_json)


@files_app.command("detach")
def f_detach(file_id: str, reference: str = typer.Option(..., "--reference"), hub: Path = typer.Option(Path("."), "--hub"),
             user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        _svc(loc).detach(loc.actor_obj, file_id, reference)
    cc.emit({"detached": reference}, as_json)


@files_app.command("delete")
def f_delete(file_id: str, hub: Path = typer.Option(Path("."), "--hub"),
             expected_revision: int = typer.Option(None, "--expected-revision"), user: str = USER,
             as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).delete(loc.actor_obj, file_id, expected_revision), as_json)


@files_app.command("doctor")
def f_doctor(hub: Path = HUB, as_json: bool = JSON) -> None:
    """Write, read and delete a synthetic object with the configured storage."""
    loc = _local(hub, as_json, None)
    report = _svc(loc).doctor()
    cc.emit(report, as_json)
    if not report["ok"]:
        raise typer.Exit(5)


@artifacts_app.command("list")
def a_list(hub: Path = HUB, conversation: str = typer.Option(..., "--conversation"), user: str = USER,
           as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit({"items": _svc(loc).list_artifacts(loc.actor_obj, conversation)}, as_json)


@artifacts_app.command("show")
def a_show(artifact_id: str, hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).get_artifact(loc.actor_obj, artifact_id), as_json)


@artifacts_app.command("download")
def a_download(artifact_id: str, output: Path = typer.Option(..., "--output"),
               version: str = typer.Option(None, "--version"), hub: Path = typer.Option(Path("."), "--hub"),
               user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        _art, ver, data = _svc(loc).version_content(loc.actor_obj, artifact_id, version)
    output.write_bytes(data)
    cc.emit({"saved_to": str(output), "bytes": len(data), "version": ver}, as_json)


@artifacts_app.command("revise")
def a_revise(artifact_id: str, file: Path = typer.Option(..., "--file", help="The new version's content."),
             base_version: str = typer.Option(..., "--base-version"),
             expected_revision: int = typer.Option(..., "--expected-revision"),
             hub: Path = typer.Option(Path("."), "--hub"), user: str = USER, as_json: bool = JSON) -> None:
    loc = _local(hub, as_json, user)
    with loc.guard():
        cc.emit(_svc(loc).revise(loc.actor_obj, artifact_id, content=file.read_bytes(), base_version_id=base_version,
                                 expected_revision=expected_revision), as_json)
