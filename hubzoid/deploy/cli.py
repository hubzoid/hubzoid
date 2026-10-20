"""`hubzoid deploy init` and `hubzoid deploy verify`."""
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from . import PROVIDERS

console = Console()

deploy_app = typer.Typer(
    help="Host a hub on a provider: write its files (`init railway|maritime`), "
         "then check the deployment from outside (`verify URL`).",
    no_args_is_help=True)


@deploy_app.command("init")
def init_cmd(
    provider: str = typer.Argument(..., help=f"One of: {', '.join(PROVIDERS)}."),
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    image: str = typer.Option(None, "--image", help="The Hubzoid image to build on. Default: this release's."),
    force: bool = typer.Option(False, "--force", help="Replace files that already exist and differ."),
    as_json: bool = typer.Option(False, "--json", help="Print what was written as JSON."),
) -> None:
    """Write the Dockerfile, .dockerignore and deploy/<provider>/ (steps and
    variables) for hosting this hub. Never replaces a file without --force."""
    from . import scaffold

    hub = hub.resolve()
    try:
        written = scaffold.write(hub, provider.lower(), image=image, force=force)
    except scaffold.ScaffoldError as exc:
        if as_json:
            print(json.dumps({"detail": {"code": "invalid_request", "message": str(exc)}}))
        else:
            console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    if as_json:
        print(json.dumps({"provider": provider, "hub": str(hub),
                          "files": [{"path": w.path, "action": w.action} for w in written]}, indent=2))
        return
    for w in written:
        console.print(f"  {w.action:9} {w.path}")
    console.print(f"Next: read deploy/{provider}/README.md, commit these files, and follow its steps. "
                  f"Then check it with `hubzoid deploy verify <address>`.")


@deploy_app.command("verify")
def verify_cmd(
    url: str = typer.Argument(..., help="The deployment's public address, e.g. https://your-hub.up.railway.app"),
    allow_http: bool = typer.Option(False, "--allow-http", help="Warn, not fail, for plain HTTP (a local test)."),
    as_json: bool = typer.Option(False, "--json", help="Print the checks as JSON."),
) -> None:
    """Check a hosted hub from outside: HTTPS, the health check, that app data
    needs sign-in, and that HUBZOID_PUBLIC_URL is this address. Exits 1 when a
    check fails."""
    from . import verify

    try:
        results = verify.run(url, allow_http=allow_http)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    failed = any(r.status == "fail" for r in results)
    if as_json:
        print(json.dumps({"url": url, "ok": not failed, "checks": [r.public() for r in results]}, indent=2))
    else:
        marks = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]"}
        for r in results:
            console.print(f"{marks[r.status]} {escape(r.summary)} [dim]({r.id})[/dim]")
            if r.fix and r.status != "ok":
                console.print(f"    [dim]{escape(r.fix)}[/dim]")
        console.print("[green]Ready.[/green]" if not failed else "[red]Not ready.[/red]")
    if failed:
        raise typer.Exit(1)
