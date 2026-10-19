"""``hubzoid workflow-access``: who may run, schedule, edit, approve and see each workflow.

    hubzoid workflow-access resources ./hub
    hubzoid workflow-access export ./hub --output workflows/access.yaml
    hubzoid workflow-access validate ./hub --file workflows/access.yaml
    hubzoid workflow-access diff ./hub --file workflows/access.yaml
    hubzoid workflow-access apply ./hub --file workflows/access.yaml --expected-revision 3
    hubzoid workflow-access explain ./hub --subject ana@example.org --workflow weekly_report --action edit

Editing ``workflows/access.yaml`` changes nothing by itself: ``apply`` makes it
the policy, as the account given by ``--user`` (default: the local owner).
"""
from __future__ import annotations

from pathlib import Path

import typer

from ..control import cli_common as cc
from . import access as wf

app = typer.Typer(help="Who may run, schedule, edit, approve and see each workflow.", no_args_is_help=True)
HUB = typer.Argument(Path("."), help="Hub folder. Default: current folder.")
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
    except wf.AccessError as exc:
        cc.fail(exc.status, exc.code, exc.message, as_json, **exc.extra)


@app.command("resources")
def resources(hub: Path = HUB, user: str = USER, as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    me = _who(hub, user)
    items = [dict(r, allowed_actions=wf.allowed_actions(hub, me, r["key"])) for r in wf.resources(hub)]
    cc.emit({"items": items}, as_json, human=lambda o: typer.echo("\n".join(
        f"{r['key']:<28} {r['kind']:<9} you: {', '.join(r['allowed_actions']) or 'nothing'}" for r in o["items"])
        or "This hub has no workflows."))


@app.command("export")
def export(hub: Path = HUB, output: Path = typer.Option(None, "--output", help="Write the file here."),
           as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    text = wf.dump(wf.explicit(hub))
    if output:
        out = output if output.is_absolute() else hub / output
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
    if as_json:
        cc.emit({"revision": wf.revision(hub), "yaml": text}, True)
    else:
        typer.echo(f"# revision {wf.revision(hub)}\n{text}" if not output else f"Wrote {output} (revision "
                                                                                 f"{wf.revision(hub)}).")


def _read(hub: Path, file: Path) -> str:
    return (file if file.is_absolute() else hub / file).read_text()


@app.command("validate")
def validate(hub: Path = HUB, file: Path = typer.Option(Path(wf.FILE), "--file"), as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    rows = _run(wf.validate, as_json, hub, _read(hub, file))
    cc.emit({"valid": True, "grants": len(rows)}, as_json)


@app.command("diff")
def diff(hub: Path = HUB, file: Path = typer.Option(Path(wf.FILE), "--file"), as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    out = _run(wf.diff, as_json, hub, _read(hub, file))
    cc.emit(out, as_json, human=lambda o: typer.echo("\n".join(
        [f"+ {g['subject']} {g['workflow']} {g['action']}" for g in o["added"]]
        + [f"- {g['subject']} {g['workflow']} {g['action']}" for g in o["removed"]]) or "No change."))


@app.command("apply")
def apply(hub: Path = HUB, file: Path = typer.Option(Path(wf.FILE), "--file"),
          expected_revision: int = typer.Option(..., "--expected-revision"), user: str = USER,
          as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    out = _run(wf.apply, as_json, hub, _read(hub, file), expected_revision=expected_revision,
               actor=_who(hub, user))
    cc.emit(out, as_json, human=lambda o: typer.echo(
        f"Applied revision {o['revision']}: {len(o['added'])} added, {len(o['removed'])} removed."))


@app.command("explain")
def explain(hub: Path = HUB, subject: str = typer.Option(None, "--subject"),
            workflow: str = typer.Option(..., "--workflow"), action: str = typer.Option(..., "--action"),
            user: str = USER, as_json: bool = JSON) -> None:
    hub = _hub(hub, as_json)
    me = _who(hub, user)
    out = _run(wf.explain, as_json, hub, asker=me, subject=subject or me, workflow=workflow, action=action)
    cc.emit(out, as_json, human=lambda o: typer.echo(
        f"{o['subject']} {'may' if o['allowed'] else 'may not'} {o['action']} {o['workflow']}: "
        + ("; ".join(o["sources"]) if o["allowed"] else o["reason"])))
