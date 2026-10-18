"""`hubzoid stations ...` and `hubzoid handoffs ...`.

Same rules and errors as the API. Exit codes as `hubzoid work-items`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.markup import escape

from ..work_items.cli import _fail, _hub, _operator, _out, _run, console

stations_app = typer.Typer(no_args_is_help=True, help="Isolated agents (stations): validate and inspect calls.")
handoffs_app = typer.Typer(no_args_is_help=True, help="Handoff documents between stations of a work item.")
calls_app = typer.Typer(no_args_is_help=True, help="Station calls of a work item.")
stations_app.add_typer(calls_app, name="calls")


@stations_app.command("validate")
def validate(hub: Path = typer.Argument(Path(".")), as_json: bool = typer.Option(False, "--json")) -> None:
    """Check every isolated agent: engine, tools, schema and sandbox. Nothing runs."""
    from . import definitions

    hub = _hub(hub)
    out = definitions.validate(hub)

    def render(v):
        for check in v["checks"]:
            mark = "[green]ok[/green]" if check["status"] == "pass" else "[red]fail[/red]"
            console.print(f"{mark} {escape(check['message'])}" + (f" [dim]({escape(check['path'])})[/dim]" if check["path"] else ""))

    _out(out, as_json, render)
    if not out["valid"]:
        raise typer.Exit(2)


@stations_app.command("doctor")
def doctor(hub: Path = typer.Argument(Path(".")), as_json: bool = typer.Option(False, "--json")) -> None:
    """Each station's digest, engine, model, exact tools, schema, and whether its
    engine's login is available. Never prints a credential."""
    from . import definitions

    hub = _hub(hub)
    out = definitions.validate(hub)
    from ..factory_codex import codex_available

    claude_login = Path.home().joinpath(".claude.json").exists() or bool(__import__("os").environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    report = {"valid": out["valid"], "checks": out["checks"], "stations": {}}
    for name, s in (out.get("stations") or {}).items():
        report["stations"][name] = {
            "digest": s["digest"], "engine": s["engine"], "model": s["model"] or "(the engine's default)",
            "tools": s["tools"], "structured_output": "native" if s["schema"] else "text",
            "sandbox": s["sandbox"], "input": s["input"],
            "credentials_available": codex_available() if s["engine"] == "codex-local" else claude_login,
        }

    def render(v):
        for name, s in v["stations"].items():
            console.print(f"[bold]{name}[/bold] {s['engine']} {s['model']} · tools {escape('[' + (', '.join(s['tools']) or 'none') + ']')} · "
                          f"{s['structured_output']} output · login {'available' if s['credentials_available'] else 'missing'}"
                          f" · {s['digest'][:23]}")
        if not v["stations"]:
            console.print("No isolated agents yet.")

    _out(report, as_json, render)


@calls_app.command("list")
def calls_list(hub: Path = typer.Argument(Path(".")), work_item: str = typer.Option(..., "--work-item"),
               as_json: bool = typer.Option(False, "--json")) -> None:
    """Station calls of one work item."""
    from . import service

    hub = _hub(hub)
    out = _run(service.view_for, as_json, hub, work_item, _operator())

    def render(v):
        for c in v["calls"]:
            console.print(f"{c['id']}  {c['agent']}  stage {c['stage']}  [bold]{c['status']}[/bold]"
                          + (f" ({c['error_code']})" if c.get("error_code") else "")
                          + f"  retry {'used' if c['retry_used'] else 'available'}")
        if not v["calls"]:
            console.print("No station calls yet.")

    _out(out, as_json, render)


@calls_app.command("show")
def calls_show(hub: Path = typer.Argument(Path(".")), work_item: str = typer.Option(..., "--work-item"),
               call: str = typer.Option(..., "--call"), as_json: bool = typer.Option(False, "--json")) -> None:
    """One station call: attempts, pinned definition digest, output or error."""
    from . import service

    hub = _hub(hub)
    out = _run(service.view_for, as_json, hub, work_item, _operator())
    match = next((c for c in out["calls"] if c["id"] == call), None)
    if match is None:
        from ..work_items.errors import not_found
        _fail(not_found("station call"), as_json)
    _out(match, as_json)


@handoffs_app.command("list")
def handoffs_list(hub: Path = typer.Argument(Path(".")), work_item: str = typer.Option(..., "--work-item"),
                  as_json: bool = typer.Option(False, "--json")) -> None:
    """Handoff documents of one work item."""
    from . import service

    hub = _hub(hub)
    out = _run(service.view_for, as_json, hub, work_item, _operator())
    _out({"handoffs": out["handoffs"]}, as_json,
         lambda v: [console.print(f"{h['id']}  {h['kind']}  {h.get('title', '')}") for h in v["handoffs"]]
         or (not v["handoffs"] and console.print("No handoffs yet.")))


@handoffs_app.command("read")
def handoffs_read(hub: Path = typer.Argument(Path(".")), work_item: str = typer.Option(..., "--work-item"),
                  id_: str = typer.Option(..., "--id"), as_json: bool = typer.Option(False, "--json")) -> None:
    """One handoff document, as text."""
    from . import service

    hub = _hub(hub)
    _run(service.view_for, as_json, hub, work_item, _operator())
    doc = _run(service.read_handoff, as_json, hub, work_item, id_)
    _out(doc, as_json, lambda v: console.print(f"[bold]{v['title']}[/bold] ({v['kind']})\n\n{v['markdown']}"))
