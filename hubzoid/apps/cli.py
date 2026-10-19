"""`hubzoid apps ...`: cards (MCP Apps) from the files, the CLI or the API.

The same functions, rules and error codes as ``/portal/api/hubs/{hub}/apps``.
Exit codes as ``hubzoid work-items``: 2 invalid, 3 denied, 4 conflict.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer
from rich.markup import escape

from ..work_items.cli import _fail, _hub, _operator, _out, _run, console
from ..work_items.errors import WorkItemError

apps_app = typer.Typer(no_args_is_help=True, help="Cards: tool results shown as MCP Apps.")
config_app = typer.Typer(no_args_is_help=True, help="The active card configuration.")
apps_app.add_typer(config_app, name="config")


def _person(hub: Path, as_: Optional[str]) -> str:
    from ..access import normalize
    from ..access.session import configured_owner

    return normalize(as_) if as_ else configured_owner(hub)


def _read_json(path: Optional[Path], as_json: bool) -> dict:
    if path is None:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(WorkItemError("invalid_request", f"{path} is not a JSON file: {exc}"), as_json)
    if not isinstance(value, dict):
        _fail(WorkItemError("invalid_request", f"{path} must hold a JSON object."), as_json)
    return value


@config_app.command("get")
def config_get(hub: Path = typer.Argument(Path(".")), as_json: bool = typer.Option(False, "--json")) -> None:
    """The active revision, its digest and cards."""
    from . import cards

    out = _run(cards.get, as_json, _hub(hub))

    def render(v):
        if v["revision"] is None:
            console.print("No card configuration is applied (revision: absent).")
            return
        console.print(f"Revision {v['revision']}  {v['digest']}  applied by {v['applied_by']}"
                      + ("" if v["enabled"] else "  [yellow](cards disabled)[/yellow]"))
        for c in v["cards"]:
            console.print(f"  {c['id']}: {c['kind']} card for {c['tool']}  {c['resource_uri']}")

    _out(out, as_json, render)


@config_app.command("set")
def config_set(hub: Path = typer.Argument(Path(".")), pointer: str = typer.Argument(...),
               value: str = typer.Argument(..., help="A JSON value, such as false or [\"table\"]."),
               expected_revision: Optional[str] = typer.Option(None, "--expected-revision",
                                                               help="Apply right away against this revision."),
               as_json: bool = typer.Option(False, "--json")) -> None:
    """Set one value in mcp-apps.yaml by JSON Pointer (arrays are replaced, not
    appended). With --expected-revision, apply the result too."""
    from . import config

    hub = _hub(hub)
    try:
        parsed = json.loads(value)
    except ValueError:
        _fail(WorkItemError("invalid_request", "VALUE must be JSON (quote text: '\"text\"')."), as_json)
    try:
        written = config.set_pointer(hub, pointer, parsed)
    except Exception as exc:  # noqa: BLE001 - shown as a validation error
        _fail(WorkItemError("config_invalid", f"Not set: {str(exc).splitlines()[0]}", status=422), as_json)
    if expected_revision is not None:
        return apply(hub, expected_revision, as_json)
    _out({"written": written, "pointer": pointer, "value": parsed}, as_json,
         lambda v: console.print(f"Set {pointer} in {written}. Apply it with hubzoid apps apply."))


@apps_app.command("validate")
def validate(hub: Path = typer.Argument(Path(".")), check: bool = typer.Option(
                 False, "--check", help="Exit 2 when the configuration has errors."),
             as_json: bool = typer.Option(False, "--json")) -> None:
    """Check mcp-apps.yaml and cards/*.yaml against the hub's tools. Nothing changes."""
    from . import cards

    hub = _hub(hub)
    out = cards.summary(hub, cards.validate(hub))

    def render(v):
        if not v["present"]:
            console.print("No mcp-apps.yaml: no tool shows a card.")
            return
        for e in v["errors"]:
            where = ", ".join(str(x) for x in (e.get("file"), e.get("pointer"),
                                                f"line {e['line']}" if e.get("line") else None) if x)
            console.print(f"[red]{e['code']}[/red] {escape(e['message'])} [dim]{escape(where)}[/dim]")
        for c in v["resolved_resources"]:
            console.print(f"{c['id']}: {c['kind']} card for {c['tool']}  {c['resource_uri']}")
        console.print("[green]Valid.[/green]" if v["valid"] else "[red]Not valid: nothing was applied.[/red]")

    _out(out, as_json, render)
    if check and not out["valid"]:
        raise typer.Exit(2)


@apps_app.command("apply")
def apply(hub: Path = typer.Argument(Path(".")),
          expected_revision: str = typer.Option(..., "--expected-revision",
                                                help="The active revision you reviewed, or 'absent'."),
          as_json: bool = typer.Option(False, "--json")) -> None:
    """Make the checked files the next active revision (compare-and-set)."""
    from . import cards

    hub = _hub(hub)
    expected = None if expected_revision == "absent" else int(expected_revision)
    out = _run(cards.apply, as_json, hub, expected=expected, actor=_operator())
    _out(out, as_json, lambda v: console.print(f"Applied revision {v['revision']}  {v['digest']}"))


@apps_app.command("list")
def list_(hub: Path = typer.Argument(Path(".")), as_: Optional[str] = typer.Option(None, "--as"),
          as_json: bool = typer.Option(False, "--json")) -> None:
    """Recent cards made for a person (--as, default the hub's owner)."""
    from . import cards, store

    hub = _hub(hub)
    rows = _run(store.list_instances, as_json, hub, cards.hub_key(hub), requester=_person(hub, as_))

    def render(v):
        if not v:
            console.print("No cards yet.")
        for r in v:
            console.print(f"{r['id']}  {r['card_id']}  {r['tool']}" + ("  [red]revoked[/red]" if r["revoked"] else ""))

    _out(rows, as_json, render)


@apps_app.command("invoke")
def invoke(hub: Path = typer.Argument(Path(".")), card_id: str = typer.Argument(...),
           args_file: Optional[Path] = typer.Option(None, "--args-file"),
           idempotency_key: str = typer.Option(..., "--idempotency-key"),
           as_: Optional[str] = typer.Option(None, "--as", help="Run as this person (default the hub's owner)."),
           as_json: bool = typer.Option(False, "--json")) -> None:
    """Run a card's tool for real, under that person's grants, and show the card."""
    from ..work_items import store as wi_store
    from .routes import InvokeBody, _invoke

    hub = _hub(hub)
    body = InvokeBody(card_id=card_id, arguments=_read_json(args_file, as_json))
    person = _person(hub, as_)
    status, out = _run(wi_store.idempotent, as_json, hub, person, f"{hub.name.lower()}:apps.invoke",
                       idempotency_key, body.model_dump(), lambda: (200, asyncio.run(_invoke(hub, person, body))))

    def render(v):
        console.print(v["text"])
        if v["instance"]:
            console.print(f"\n[dim]card {v['instance']['id']}  {v['instance']['resource_uri']}[/dim]")
        else:
            console.print("\n[yellow]No card: the result did not fit the card (see the bridge log).[/yellow]")

    _out(out, as_json, render)


@apps_app.command("inspect")
def inspect(hub: Path = typer.Argument(Path(".")), instance_id: str = typer.Argument(...),
            format: str = typer.Option("text", "--format", help="text or json")) -> None:
    """A card's result, text, state and binding, without a browser."""
    from . import cards

    hub = _hub(hub)
    as_json = format == "json"
    out = _run(cards.view, as_json, hub, instance_id, _operator())

    def render(v):
        console.print(v["text"])
        console.print(f"\n[dim]{v['kind']} card {v['card_id']} from {v['tool']}, for {v['requester']}; "
                      f"revision {v['config_revision']}; resource {v['resource_sha256'][:12]}…[/dim]")
        if v.get("status"):
            console.print(f"State now: {v['status'].get('label')}")
        if not v["live"]:
            console.print(f"[yellow]Not live: {v['inactive_reason']}[/yellow]")

    _out(out, as_json, render)


@apps_app.command("dispatch")
def dispatch(hub: Path = typer.Argument(Path(".")), instance: str = typer.Option(..., "--instance"),
             method: str = typer.Option(..., "--method"), params_file: Optional[Path] = typer.Option(
                 None, "--params-file"), as_: Optional[str] = typer.Option(None, "--as"),
             as_json: bool = typer.Option(False, "--json")) -> None:
    """Send one card request (as the card frame would). No decision authority."""
    from .. import mcp_server
    from . import cards
    from .routes import _identity

    hub = _hub(hub)
    person = _person(hub, as_)
    params = _read_json(params_file, as_json)

    async def run():
        ident = _identity(hub, person)
        return await cards.dispatch(hub, instance, person, method, params, identity=ident,
                                    call_tool=lambda n, a, i, c: mcp_server.call_registry_tool(hub, n, a, i, c))

    out = _run(lambda: asyncio.run(run()), as_json)
    _out(out, as_json)


@apps_app.command("revoke")
def revoke(hub: Path = typer.Argument(Path(".")), instance_id: str = typer.Argument(...),
           reason: str = typer.Option("revoked", "--reason"), as_json: bool = typer.Option(False, "--json")) -> None:
    """Stop a card from making any further request."""
    from . import cards

    hub = _hub(hub)
    out = _run(cards.revoke, as_json, hub, instance_id, _operator(), reason)
    _out(out, as_json, lambda v: console.print(f"Revoked {v['id']}."))


@apps_app.command("probe")
def probe(hub: Path = typer.Argument(Path(".")), client_capabilities: str = typer.Option(
              "none", "--client-capabilities", help="'none', 'mcp-apps', or a JSON file of client capabilities."),
          as_json: bool = typer.Option(False, "--json")) -> None:
    """How a client would get each card (rendered or text). Not a host claim."""
    from . import cards

    hub = _hub(hub)
    if client_capabilities == "none":
        caps: dict = {}
    elif client_capabilities == "mcp-apps":
        caps = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}
    else:
        caps = _read_json(Path(client_capabilities), as_json)
    out = _run(cards.probe, as_json, hub, caps)

    def render(v):
        console.print(f"Rendering: {v['mode']}")
        for c in v["cards"]:
            console.print(f"  {c['card_id']} ({c['tool']}): {c['mode']}, text fallback always")
        console.print(f"[dim]{v['note']}[/dim]")

    _out(out, as_json, render)
