"""`hubzoid sandbox ...`: check, prepare, probe and clean up sandboxes.

Same rules and error codes as the API. Exit codes as `hubzoid work-items`:
0 done, 1 failed, 2 invalid, 3 denied, 4 conflict, 5 unavailable (including
`sandbox_lost` and Docker down).
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import typer
from rich.markup import escape

from ..work_items.cli import _fail, _hub, _operator, _out, _run, console
from ..work_items.errors import WorkItemError

sandbox_app = typer.Typer(no_args_is_help=True, help="Sandboxes for agents: doctor, profiles, probes, cleanup.")
profiles_app = typer.Typer(no_args_is_help=True, help="Sandbox profiles (sandboxes/<id>.yaml).")
sandbox_app.add_typer(profiles_app, name="profiles")


def _when(ts) -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


@sandbox_app.command("doctor")
def doctor(hub: Path = typer.Argument(Path(".")), as_json: bool = typer.Option(False, "--json")) -> None:
    """Is Docker reachable, is every profile valid and prepared? Creates nothing."""
    from . import service

    report = service.doctor(_hub(hub))

    def render(v):
        for c in v["checks"]:
            color = {"pass": "green", "warn": "yellow", "fail": "red"}[c["status"]]
            console.print(f"[{color}]{c['status']}[/{color}] {escape(c['summary'])}"
                          + (f"\n      {escape(c['recovery_action'])}" if c.get("recovery_action") else ""))

    _out(report, as_json, render)
    if not report["valid"]:
        raise typer.Exit(5 if any(c["id"].startswith("provider") and c["status"] == "fail"
                                  for c in report["checks"]) else 2)


@sandbox_app.command("config")
def config_show(hub: Path = typer.Argument(Path(".")), as_json: bool = typer.Option(False, "--json")) -> None:
    """The effective sandbox.yaml."""
    from . import config

    cfg = _run(config.load_deployment, as_json, _hub(hub))
    _out({"present": cfg.present, "digest": cfg.digest or None, "effective": cfg.to_dict()}, as_json)


@profiles_app.command("list")
def profiles_list(hub: Path = typer.Argument(Path(".")), as_json: bool = typer.Option(False, "--json")) -> None:
    """Profiles in sandboxes/."""
    from . import config

    hub = _hub(hub)
    out = []
    for pid in config.list_profiles(hub):
        try:
            p = config.load_profile(hub, pid)
            out.append({"id": pid, "valid": True, "digest": p.digest, "description": p.description})
        except WorkItemError as exc:
            out.append({"id": pid, "valid": False, "error": exc.message})
    _out({"profiles": out}, as_json, lambda v: [console.print(
        f"{p['id']}  {'valid ' + p['digest'][:19] if p['valid'] else '[red]' + escape(p['error']) + '[/red]'}")
        for p in v["profiles"]] or console.print("No profiles in sandboxes/."))


@profiles_app.command("validate")
def profiles_validate(hub: Path = typer.Argument(Path(".")), as_json: bool = typer.Option(False, "--json")) -> None:
    """Check sandbox.yaml and every profile. Nothing runs."""
    from . import config

    hub = _hub(hub)
    checks = []
    try:
        deployment = config.load_deployment(hub)
        checks.append({"code": "ok", "path": config.DEPLOYMENT_FILE if deployment.present else None,
                       "status": "pass", "message": "sandbox.yaml is valid" if deployment.present
                       else "No sandbox.yaml: sandboxes are off."})
    except WorkItemError as exc:
        deployment = None
        checks.append({"code": exc.code, "path": exc.fields.get("file"), "status": "fail", "message": exc.message})
    for pid in config.list_profiles(hub):
        try:
            profile = config.load_profile(hub, pid)
            if deployment is not None and deployment.enabled:
                config.check_profile(hub, profile, deployment)
            checks.append({"code": "ok", "path": profile.source, "status": "pass",
                           "message": f"{pid}: {profile.digest[:19]}, setup "
                                      f"{'allowlist of ' + str(len(profile.setup_hosts())) + ' hosts' if profile.setup and profile.setup_hosts() else 'offline'}"
                                      ", agent commands offline"})
        except WorkItemError as exc:
            checks.append({"code": exc.code, "path": exc.fields.get("file"), "status": "fail", "message": exc.message})
    report = {"valid": all(c["status"] == "pass" for c in checks), "checks": checks}
    _out(report, as_json, lambda v: [console.print(
        f"{'[green]pass[/green]' if c['status'] == 'pass' else '[red]fail[/red]'} {escape(c['message'])}")
        for c in v["checks"]])
    if not report["valid"]:
        raise typer.Exit(2)


@profiles_app.command("prepare")
def profiles_prepare(hub: Path = typer.Argument(Path(".")), profile: str = typer.Argument(...),
                     as_json: bool = typer.Option(False, "--json")) -> None:
    """Build or pull the profile's image (and the setup proxy) ahead of use."""
    from . import service

    out = _run(service.prepare, as_json, _hub(hub), profile)
    _out(out, as_json, lambda v: console.print(f"Prepared {v['profile']}: {v['image']}"))


@sandbox_app.command("try")
def try_command(hub: Path = typer.Argument(Path(".")), profile: str = typer.Option(..., "--profile"),
                command: List[str] = typer.Argument(None, help="The command, after --."),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """Run one command in a fresh sandbox of PROFILE, after its setup, with
    agent commands' network (offline). The sandbox is deleted afterwards."""
    from . import service

    out = _run(service.probe, as_json, _hub(hub), profile, list(command or []), actor=_operator())

    def render(v):
        console.print(escape(v["stdout"]), end="")
        if v["stderr"]:
            console.print(f"[yellow]{escape(v['stderr'])}[/yellow]", end="")
        console.print(f"[dim]exit {v['exit_code']} · network {v['network']} · sandbox {v['sandbox_id']} "
                      f"deleted[/dim]")

    _out(out, as_json, render)
    if out["exit_code"] != 0:
        raise typer.Exit(1)


@sandbox_app.command("list")
def list_cmd(hub: Path = typer.Argument(Path(".")), state: Optional[str] = typer.Option(None, "--state"),
             owner_kind: Optional[str] = typer.Option(None, "--owner-kind"),
             limit: int = typer.Option(50, "--limit"), as_json: bool = typer.Option(False, "--json")) -> None:
    """Sandboxes of this hub, newest first."""
    from . import service

    out = _run(service.list_view, as_json, _hub(hub), state=state, owner_kind=owner_kind, limit=limit)

    def render(v):
        for s in v["sandboxes"]:
            console.print(f"{s['id']}  {s['owner_kind']}:{s['owner_id'][:12]}  {s['profile_id']}  "
                          f"[bold]{s['state']}[/bold]" + (f" ({s['end_reason']})" if s.get("end_reason") else "")
                          + f"  {_when(s['created'])}")
        if not v["sandboxes"]:
            console.print("No sandboxes.")

    _out(out, as_json, render)


@sandbox_app.command("show")
def show(hub: Path = typer.Argument(Path(".")), sandbox_id: str = typer.Argument(...),
         as_json: bool = typer.Option(False, "--json")) -> None:
    """One sandbox: owner, profile, state, network policy and setup result. No file contents."""
    from . import service

    row = _run(service.require, as_json, _hub(hub), sandbox_id)
    _out(service.view(row), as_json)


@sandbox_app.command("stop")
def stop(hub: Path = typer.Argument(Path(".")), sandbox_id: str = typer.Argument(...),
         reason: str = typer.Option(..., "--reason"), expected_revision: Optional[int] = typer.Option(
             None, "--expected-revision"), as_json: bool = typer.Option(False, "--json")) -> None:
    """Stop a sandbox's processes. Its files are kept; its owner resumes it."""
    from . import service

    out = _run(service.operator_stop, as_json, _hub(hub), sandbox_id, _operator(), reason=reason,
               expected_revision=expected_revision)
    _out(out, as_json, lambda v: console.print(f"Sandbox {v['id']} is {v['state']}."))


@sandbox_app.command("delete")
def delete(hub: Path = typer.Argument(Path(".")), sandbox_id: str = typer.Argument(...),
           reason: str = typer.Option(..., "--reason"), expected_revision: Optional[int] = typer.Option(
               None, "--expected-revision"), as_json: bool = typer.Option(False, "--json")) -> None:
    """Delete a sandbox. Its owner sees sandbox_lost. Hub files and outputs stay."""
    from . import service

    out = _run(service.operator_delete, as_json, _hub(hub), sandbox_id, _operator(), reason=reason,
               expected_revision=expected_revision)
    _out(out, as_json, lambda v: console.print(f"Sandbox {v['id']} is {v['state']}."))


@sandbox_app.command("reap")
def reap(hub: Path = typer.Argument(Path(".")), execute: bool = typer.Option(
             False, "--execute", help="Do it. Without this flag, only show what would happen."),
         as_json: bool = typer.Option(False, "--json")) -> None:
    """Stop idle sandboxes, remove finished or expired ones, and delete
    leftover containers and networks this deployment labelled."""
    from . import service

    out = _run(service.reap, as_json, _hub(hub), dry_run=not execute)

    def render(v):
        verb = "Would" if v["dry_run"] else "Did"
        console.print(f"{verb} stop {len(v['stop'])}, remove {len(v['release'])}, "
                      f"delete {len(v['orphans'])} orphaned resources.")
        for key in ("stop", "release", "orphans"):
            for e in v[key]:
                console.print(f"  {key}: {e['id']} {e.get('reason') or e.get('role') or ''}")

    _out(out, as_json, render)
