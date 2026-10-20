"""``hubzoid hub-changes``: proposed changes to a hub's own files.

    hubzoid hub-changes list ./hub [--status pending|applied|attention]
    hubzoid hub-changes show ./hub 3
    hubzoid hub-changes propose ./hub --title "..." --edits edits.json
    hubzoid hub-changes decide ./hub 3          (in your own terminal: shows the diff, asks for a code)
    hubzoid hub-changes undo ./hub 3

Acts as the account given by ``--user`` (default: the local owner).
"""
from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

import typer

from ..control import cli_common as cc
from . import service

app = typer.Typer(help="Proposed changes to a hub's own instructions and knowledge.", no_args_is_help=True)
HUB = typer.Argument(..., help="Hub folder.")
CID = typer.Argument(..., help="Change number.")
JSON = typer.Option(False, "--json", help="Print the API's JSON object.")
USER = typer.Option(None, "--user", help="Act as this account (email). Default: the local owner.")


def _hub(hub: Path, as_json: bool) -> Path:
    hub = Path(hub).resolve()
    if not (hub / "AGENTS.md").is_file():
        cc.fail(400, "invalid_request", f"{hub} is not a hub folder.", as_json)
    from .. import migrations
    from ..db import operational_engine

    migrations.upgrade(operational_engine(hub), "operational")
    return hub


def _who(hub: Path, user: str | None) -> str:
    if user:
        return user.strip().lower()
    from .. import auth

    return auth.local_owner(hub).email.lower()


def _run(fn, as_json: bool, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except service.ChangeError as exc:
        cc.fail(exc.status, exc.code, exc.message, as_json, **exc.extra)


def _line(c: dict) -> str:
    extra = f"  {c['commit_sha'][:10]}" if c.get("commit_sha") else ""
    return f"#{c['id']:<4} {c['status']:<15} {c['title']}  ({', '.join(c['paths'])}){extra}"


@app.command("list")
def list_(hub: Path = HUB, status: str = typer.Option(None, "--status"), as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    items = [service.public(c, with_diff=False) for c in service.list_(hub, status=status)]
    cc.emit({"items": items}, as_json, human=lambda o: typer.echo(
        "\n".join(_line(c) for c in o["items"]) or "No changes."))


@app.command("show")
def show(hub: Path = HUB, change_id: int = CID, as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    ch = service.get(hub, change_id)
    if ch is None:
        cc.fail(404, "not_found", f"There is no change #{change_id}.", as_json)
    out = service.public(ch)
    cc.emit(out, as_json, human=lambda o: typer.echo(
        f"{_line(o)}\nProposed by {o['proposed_by']} via {o['proposed_via']}"
        + (f"\nWhy: {o['reason']}" if o.get("reason") else "")
        + (f"\nProblem: {o['problem']}" if o.get("problem") else "") + f"\n\n{o['diff']}"))


@app.command("propose")
def propose(hub: Path = HUB, title: str = typer.Option(..., "--title"),
            edits: Path = typer.Option(..., "--edits", help="A JSON file: a list of edits."),
            reason: str = typer.Option("", "--reason"), user: str = USER, as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    ch = _run(service.propose, as_json, hub, title=title, edits=json.loads(edits.read_text()), reason=reason,
              actor=_who(hub, user), via="cli", present=sys.stdin.isatty())
    cc.emit(service.public(ch, with_diff=False), as_json, human=lambda o: typer.echo(_line(o)))


@app.command("decide")
def decide(hub: Path = HUB, change_id: int = CID, decision: str = typer.Option(None, "--decision",
           help="approve or reject"), note: str = typer.Option(None, "--note"), user: str = USER,
           as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    ch = service.get(hub, change_id)
    if ch is None:
        cc.fail(404, "not_found", f"There is no change #{change_id}.", as_json)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        cc.fail(403, "human_action_required", "A person must decide this, in Settings, Hub changes, or by "
                "running this command in their own terminal.", as_json, change_id=change_id)
    typer.echo(f"{_line(service.public(ch, with_diff=False))}\n\n{service.diff(ch)}")
    decision = decision or typer.prompt("Decision (approve, reject)")
    code = secrets.token_hex(2).upper()
    if typer.prompt(f"Type {code} to confirm").strip().upper() != code:
        cc.fail(400, "invalid_request", "The code did not match. Nothing was decided.", as_json)
    out = _run(service.decide, as_json, hub, change_id, actor=_who(hub, user), decision=decision,
               expected_revision=ch["revision"], note=note)
    later = out["status"] == "applied" and service.needs_reload(out)
    cc.emit(service.public(out, with_diff=False), as_json, human=lambda o: typer.echo(
        _line(o) + ("\nA running agent uses changed instructions or skills after it restarts (or when a change "
                    "is applied from Hubzoid)." if later else "")))


@app.command("undo")
def undo(hub: Path = HUB, change_id: int = CID, user: str = USER, as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    out = _run(service.undo, as_json, hub, change_id, actor=_who(hub, user), via="cli")
    cc.emit(service.public(out, with_diff=False), as_json, human=lambda o: typer.echo(_line(o)))
