"""``hubzoid hub``: a hub's declared settings and its own doctor checks.

    hubzoid hub settings  [HUB] [--schema] [--json]
    hubzoid hub configure [HUB] [--file FILE] [--check | --apply]
                                [--expected-revision N|absent] [--json]
    hubzoid hub doctor    [HUB] [--check ID] [--json]

``configure`` validates a values file (default ``<hub>/hub.settings.yaml``) and
reports every problem at once. ``--apply`` writes it to the hub, recording a new
revision. Without either flag it only checks. Secret settings take references
(``secrets.KEY_ref: secret:...`` or ``env:...``), never values.

Exit codes: 0 done, 1 a check failed, 2 invalid input or settings, 4 the
revision changed since it was read.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()

hub_app = typer.Typer(help="A hub's settings and its own checks.", no_args_is_help=True)


def _out(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))


def _fail(code: str, message: str, exit_code: int, as_json: bool, **extra) -> None:
    if as_json:
        print(json.dumps({"detail": {"code": code, "message": message, **extra}}, default=str))
    else:
        console.print(f"[red]{escape(message)}[/red]")
    raise typer.Exit(exit_code)


def _hub(hub: Path, as_json: bool) -> Path:
    if not (hub / "AGENTS.md").is_file():
        _fail("invalid_request", f"{hub} is not a hub folder (no AGENTS.md).", 2, as_json)
    return hub.resolve()


@hub_app.command("settings")
def settings_cmd(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    schema: bool = typer.Option(False, "--schema", help="Show the declarations only."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Declared settings with their values and sources. Secrets show their reference only."""
    from . import settings as hs
    from .yamlfile import HubFileError

    hub_dir = _hub(hub, as_json)
    try:
        if schema:
            decls = [d.model_dump(exclude_none=True) for d in hs.declarations(hub_dir)]
            if as_json:
                return _out({"version": 1, "settings": decls}, True)
            for d in decls:
                console.print(f"[bold]{d['name']}[/bold] ({d.get('type', 'string')}"
                              f"{', required' if d.get('required') else ''}{', secret' if d.get('secret') else ''})"
                              f"  {escape(d.get('description', ''))}")
            return
        resolved, problems = hs.validate(hub_dir)
        rev = hs.revision(hub_dir)
    except HubFileError as exc:
        _fail("validation_failed", str(exc), 2, as_json)
    rows = [r.public() for r in resolved]
    if as_json:
        return _out({"revision": rev, "settings": rows, "problems": [p.public() for p in problems]}, True)
    if not rows:
        console.print("[dim]This hub declares no settings (no settings.yaml).[/dim]")
        return
    table = Table(box=None, pad_edge=False, header_style="bold")
    for col in ("Name", "Status", "Value", "Source"):
        table.add_column(col)
    for r in rows:
        shown = (f"{r['ref']} ({'available' if r['available'] else 'not set'})" if r["secret"]
                 else json.dumps(r["value"]) if r["value"] is not None else "")
        color = {"set": "green", "missing": "yellow", "invalid": "red"}[r["status"]]
        table.add_row(r["name"], f"[{color}]{r['status']}[/{color}]", escape(shown), r["source"] or "")
    console.print(table)
    console.print(f"[dim]revision {rev}[/dim]")
    for p in problems:
        console.print(f"[red]•[/red] {escape(p.message)}")


@hub_app.command("configure")
def configure_cmd(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    file: Path = typer.Option(None, "--file", help="Values to use. Default: the hub's hub.settings.yaml."),
    apply: bool = typer.Option(False, "--apply", help="Write the values to the hub."),
    check: bool = typer.Option(False, "--check", help="Validate and show changes. Writes nothing (the default)."),
    expected_revision: str = typer.Option(None, "--expected-revision",
                                          help="Apply only if the hub's values are still at this revision, "
                                               "or 'absent' when the hub has none yet."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Check a values file and, with --apply, write it to the hub."""
    from . import settings as hs
    from .yamlfile import HubFileError, load

    hub_dir = _hub(hub, as_json)
    if apply and check:
        _fail("invalid_request", "Use --check or --apply, not both.", 2, as_json)
    source = (file or hub_dir / hs.VALUES).resolve()
    if not source.is_file():
        _fail("invalid_request", f"{source} does not exist.", 2, as_json)
    try:
        document = load(source)
        candidate = hs.parse_values(document, source)
        current = hs.values_file(hub_dir)
        resolved, problems = hs.validate(hub_dir, candidate)
        rev = hs.revision(hub_dir)
    except HubFileError as exc:
        _fail("validation_failed", str(exc), 2, as_json)
    before = {**(current.values if current else {}), **(current.secrets if current else {})}
    after = {**candidate.values, **candidate.secrets}
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    result = {"valid": not problems, "base_revision": rev, "changed_keys": changed,
              "problems": [p.public() for p in problems], "applied": False}
    if problems:
        if as_json:
            print(json.dumps({"detail": {"code": "validation_failed", "message": "The settings are not valid.",
                                         "fields": result["problems"]}, **result}, default=str))
        else:
            for p in problems:
                console.print(f"[red]•[/red] {escape(p.message)}")
        raise typer.Exit(2)
    if apply:
        expected = None
        if expected_revision == "absent":
            expected = "absent"
        elif expected_revision is not None:
            try:
                expected = int(expected_revision)
            except ValueError:
                _fail("validation_failed", "--expected-revision is a number or absent.", 2, as_json)
        try:
            result["revision"] = hs.write(hub_dir, candidate.model_dump(), expected_revision=expected)
        except hs.Conflict as exc:
            _fail("revision_conflict", "The hub's settings changed since you read them. Check again.", 4, as_json,
                  current_revision=exc.current)
        result.update(applied=True, restart_required=True)
    if as_json:
        return _out(result, True)
    if not changed:
        console.print("[green]Valid.[/green] Nothing changes.")
    else:
        console.print(f"[green]Valid.[/green] Changes: {', '.join(changed)}.")
    if apply:
        console.print(f"Applied as revision {result['revision']}. It takes effect after the hub restarts.")
    elif changed:
        console.print("Nothing was written. Run again with --apply "
                      f"--expected-revision {rev if rev else 'absent'}.")


@hub_app.command("doctor")
def doctor_cmd(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    only: list[str] = typer.Option(None, "--check", help="Run only this hub check (repeatable)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """The hub's own checks: its settings and the checks in hub.manifest.yaml.
    `hubzoid doctor` runs these with everything else."""
    from dataclasses import asdict

    from .. import doctor
    from . import probes

    hub_dir = _hub(hub, as_json)
    if only:
        try:
            checks = [doctor.Check(f"probe.{r.id}", r.status, r.summary,
                                   {"recovery": r.recovery, "details": r.details})
                      for r in probes.run(hub_dir, only=list(only))]
        except ValueError as exc:
            _fail("validation_failed", str(exc), 2, as_json)
    else:
        checks = doctor._hub_setup_checks(hub_dir)
    failed = any(c.status == "fail" for c in checks)
    if as_json:
        print(json.dumps({"ok": not failed, "checks": [asdict(c) for c in checks]}, indent=2, default=str))
    else:
        if not checks:
            console.print("[dim]This hub declares no settings or checks.[/dim]")
        for c in checks:
            color = {"ok": "green", "info": "cyan", "warn": "yellow", "fail": "red"}.get(c.status, "white")
            console.print(f"[{color}]{c.status:5}[/{color}] {c.id}  {escape(c.summary)}")
            recovery = c.detail.get("recovery") if isinstance(c.detail, dict) else None
            if recovery and c.status in ("warn", "fail"):
                console.print(f"       [dim]{escape(str(recovery))}[/dim]")
    raise typer.Exit(1 if failed else 0)
