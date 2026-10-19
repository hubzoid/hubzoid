"""``hubzoid hitl`` (when the agent asks) and ``hubzoid requests`` (what waits).

Local mode, on the hub folder and its operational store, as one account
(``--user EMAIL``, default the local owner):

    hubzoid hitl validate ./hub --file hitl.yaml
    hubzoid hitl export ./hub
    hubzoid hitl apply ./hub --file hitl.yaml --expected-revision absent
    hubzoid requests list ./hub [--all]
    hubzoid requests show ./hub ID
    hubzoid requests decide ./hub ID          (interactive: a person only)
    hubzoid requests cancel ./hub ID

``decide`` runs only in an interactive terminal: it prints the exact request
and its fingerprint and asks the person to retype a short code before
sending. A script, an agent or a pipe gets ``human_action_required`` and the
request page link instead.
"""
from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

import typer

from ..control import cli_common as cc
from . import config as cfglib
from . import service

hitl_app = typer.Typer(help="When the agent asks a person first (hitl.yaml).", no_args_is_help=True)
requests_app = typer.Typer(help="Approvals and questions waiting for a person.", no_args_is_help=True)

HUB = typer.Argument(Path("."), help="Hub folder. Default: current folder.")
JSON = typer.Option(False, "--json", help="Print the API's JSON object.")
USER = typer.Option(None, "--user", help="Act as this account (email). Default: the local owner.")


def _hub(hub: Path, as_json: bool) -> Path:
    hub = Path(hub).resolve()
    if not (hub / "AGENTS.md").is_file():
        cc.fail(400, "invalid_request", f"{hub} is not a hub folder.", as_json)
    return hub


def _who(hub: Path, user: str | None, as_json: bool) -> tuple[str, str]:
    from .. import auth
    from ..auth import users

    if user:
        u = users.find_by_email(hub, user)
        if u is None:
            cc.fail(404, "not_found", f"No account {user!r} in this deployment.", as_json)
        return u["id"], u["email"].lower()
    owner = auth.local_owner(hub)
    return owner.id, owner.email.lower()


def _migrated(hub: Path) -> None:
    from .. import migrations
    from ..db import operational_engine

    migrations.upgrade(operational_engine(hub), "operational")


def _call(fn, as_json: bool, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except service.RequestError as exc:
        cc.fail(exc.status, exc.code, exc.message, as_json, **exc.extra)


# -- hubzoid hitl --------------------------------------------------------------------
@hitl_app.command("validate")
def h_validate(hub: Path = HUB, file: Path = typer.Option(None, "--file", help="Default: the hub's hitl.yaml."),
               as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    path = file or hub / cfglib.FILE
    if not path.is_file():
        cc.emit({"valid": True, "exists": False, "note": "No hitl.yaml: no tool asks first."}, as_json)
        return
    try:
        cfg = cfglib.parse(path.read_text(encoding="utf-8"), path=str(path))
    except cfglib.ConfigError as exc:
        cc.fail(422, "invalid_config", exc.message, as_json, path=exc.path, line=exc.line)
    from .routes import summary

    cc.emit({"valid": True, "revision": cfglib.revision_of(path.read_text(encoding="utf-8")),
             "summary": summary(cfg)}, as_json)


@hitl_app.command("export")
def h_export(hub: Path = HUB, as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    text = cfglib.current_text(hub)
    if as_json:
        cc.emit({"revision": cfglib.revision_of(text), "text": text, "exists": text is not None}, True)
    else:
        typer.echo(f"# revision: {cfglib.revision_of(text)}")
        typer.echo(text if text is not None else "# no hitl.yaml: no tool asks first")


@hitl_app.command("apply")
def h_apply(hub: Path = HUB, file: Path = typer.Option(..., "--file"),
            expected_revision: str = typer.Option(..., "--expected-revision",
                                                  help="The revision you exported, or 'absent' to create the file."),
            as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    try:
        cfg = cfglib.apply(hub, file.read_text(encoding="utf-8"), expected_revision=expected_revision)
    except cfglib.ConfigError as exc:
        if exc.path == "revision":
            cc.fail(409, "revision_conflict", exc.message, as_json,
                    current_revision=cfglib.revision_of(cfglib.current_text(hub)))
        cc.fail(422, "invalid_config", exc.message, as_json, path=exc.path, line=exc.line)
    from .routes import summary

    cc.emit({"revision": cfg.digest, "summary": summary(cfg)}, as_json)


# -- hubzoid requests ----------------------------------------------------------------
def _line(req: dict) -> str:
    what = req.get("question") or req["title"]
    return f"{req['id'][:8]}  {req['status']:<9} {req['kind']:<8} {what}"


@requests_app.command("list")
def r_list(hub: Path = HUB, all_: bool = typer.Option(False, "--all", help="Include decided requests."),
           user: str = USER, as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    _migrated(hub)
    uid, email = _who(hub, user, as_json)
    items = service.list_for(hub, hub.name.lower(), uid, email, status=None if all_ else "pending")
    cc.emit({"items": items}, as_json, human=lambda o: typer.echo(
        "\n".join(_line(r) for r in o["items"]) or "Nothing waits for you."))


@requests_app.command("show")
def r_show(request_id: str, hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    _migrated(hub)
    uid, email = _who(hub, user, as_json)
    cc.emit(_call(service.get_for, as_json, hub, hub.name.lower(), _full_id(hub, request_id), uid, email), as_json)


def _full_id(hub: Path, short: str) -> str:
    if len(short) >= 36:
        return short
    import sqlalchemy as sa

    from . import store

    with service.engine(hub).connect() as conn:
        rows = conn.execute(sa.select(store.requests.c.id).where(store.requests.c.id.like(f"{short}%"))).fetchall()
    return rows[0].id if len(rows) == 1 else short


@requests_app.command("decide")
def r_decide(request_id: str, hub: Path = HUB, user: str = USER,
             decision: str = typer.Option(None, "--decision",
                                          help="approve, approve_chat, approve_always, reject, request_changes or answer."),
             answer: str = typer.Option(None, "--answer"), note: str = typer.Option(None, "--note"),
             as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    _migrated(hub)
    uid, email = _who(hub, user, as_json)
    req = _call(service.get_for, as_json, hub, hub.name.lower(), _full_id(hub, request_id), uid, email)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        cc.fail(403, "human_action_required",
                "A person must decide this, in the Hubzoid page or by running this command in their own "
                "terminal.", as_json, request_id=req["id"], link=f"/requests#{req['id']}")
    typer.echo(json.dumps({k: req.get(k) for k in ("title", "tool", "arguments", "question", "options",
                                                  "subject", "requester_email", "digest")}, indent=2))
    if not req["allowed_actions"]:
        cc.fail(409, "already_decided", f"This request is {req['status']}.", as_json)
    decision = decision or typer.prompt(f"Decision ({', '.join(req['allowed_actions'])})")
    if decision == "answer" and not answer:
        answer = typer.prompt("Your answer")
    code = secrets.token_hex(2).upper()
    if typer.prompt(f"Type {code} to confirm").strip().upper() != code:
        cc.fail(400, "invalid_request", "The code did not match. Nothing was decided.", as_json)
    out = _call(service.decide, as_json, hub, hub.name.lower(), req["id"], account_id=uid, email=email,
                decision=decision, expected_revision=req["revision"], digest=req["digest"], answer=answer,
                note=note, via="cli")
    cc.emit(out, as_json, human=lambda o: typer.echo(f"{o['status']}: {o['title']}"))


@requests_app.command("cancel")
def r_cancel(request_id: str, hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    _migrated(hub)
    uid, email = _who(hub, user, as_json)
    out = _call(service.cancel, as_json, hub, hub.name.lower(), _full_id(hub, request_id), account_id=uid,
                email=email, reason="Cancelled from the command line.")
    cc.emit(out or {"status": "not_pending"}, as_json)
