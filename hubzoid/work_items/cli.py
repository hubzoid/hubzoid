"""`hubzoid work-items ...` and `hubzoid operations ...`.

Each command calls the same service as the HTTP API and the Console, so the
rules and the errors are identical. Commands act with the authority of whoever
can run commands on this server. `--as EMAIL` on `start` records who asked for
the work (default: the hub's workflow account). Decisions (approve, deny,
resolve) belong to that person: the CLI makes them only in local mode with
sign-in off, as the machine's single account. Elsewhere, use the Console or API.

Exit codes: 0 done, 1 failed, 2 invalid input, 3 denied, 4 conflict,
5 unavailable, 6 waiting for a person or uncertain.
"""
from __future__ import annotations

import getpass
import json
import socket
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .errors import WorkItemError

console = Console()
work_items_app = typer.Typer(no_args_is_help=True, help="Durable work items: start, watch and decide.")
operations_app = typer.Typer(no_args_is_help=True,
                             help="Recorded external writes: approve, deny and resolve.")


def _operator() -> str:
    return f"cli:{getpass.getuser()}@{socket.gethostname()}"


def _person(hub: Path, as_: Optional[str]) -> str:
    """Who asked for new work: `--as EMAIL`, else the hub's workflow account."""
    from ..access import normalize
    from ..workflows import identity

    if as_:
        return normalize(as_)
    try:
        return identity.resolve(hub, hub=hub.name.lower(), run_as=None,
                                legacy_subject="cli", what="This command").subject
    except identity.IdentityError as exc:
        _fail(WorkItemError("invalid_request", f"{exc} Pass --as EMAIL.", status=400), False)


def _decider(hub: Path, as_json: bool) -> str:
    """Who makes a decision from the CLI. A decision belongs to the person who
    asked for the work, so the CLI can make it only where that person is the
    machine's own single account: local mode with sign-in off. Elsewhere,
    decide in Settings or the API, signed in as that person."""
    from ..access.session import LOCAL_OWNER
    from ..workflows import identity

    if identity.local_quickstart(hub):
        return LOCAL_OWNER
    _fail(WorkItemError(
        "decide_as_requester",
        "Approvals and decisions are made by the person who asked for the work, signed in. "
        "Use My work in the Inbox page or the API as that person.", status=403), as_json)


def _exit_code(status: int) -> int:
    return {400: 2, 404: 2, 422: 2, 401: 3, 403: 3, 409: 4, 429: 5, 503: 5}.get(status, 1)


def _fail(exc: WorkItemError, as_json: bool):
    if as_json:
        print(json.dumps({"detail": exc.detail()}, default=str))
    else:
        console.print(f"[red]{exc.message}[/red] [dim]({exc.code})[/dim]")
    raise typer.Exit(_exit_code(exc.status))


def _out(value, as_json: bool, render=None, *, waiting: bool = False):
    if as_json:
        print(json.dumps(value, indent=2, default=str))
    elif render is not None:
        render(value)
    else:
        console.print_json(json.dumps(value, default=str))
    if waiting:
        raise typer.Exit(6)


def _hub(hub: Path) -> Path:
    hub = hub.resolve()
    if not hub.is_dir():
        console.print(f"[red]No hub folder at {hub}[/red]")
        raise typer.Exit(2)
    return hub


def _run(fn, as_json, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except WorkItemError as exc:
        _fail(exc, as_json)


def _item_line(item: dict) -> str:
    reason = f" ({item['reason']})" if item.get("reason") else ""
    return (f"{item['id']}  {item['workflow']}  {item['logical_key']}  gen {item['generation']}  "
            f"{item['state']}{reason}  rev {item['revision']}")


def _render_item(item: dict) -> None:
    console.print(f"[bold]{item['workflow']}[/bold] · {item['logical_key']} · generation {item['generation']}")
    console.print(f"  id {item['id']} · route {item['route']} · state [bold]{item['state']}[/bold]"
                  + (f" ({item['reason']})" if item.get("reason") else "") + f" · revision {item['revision']}")
    if item.get("error"):
        console.print(f"  [yellow]{item['error']}[/yellow]")
    for stage in item.get("stages") or []:
        console.print(f"  stage {stage['stage']}: {stage['status']}")
    for op in item.get("operations") or []:
        console.print(f"  operation {op['id']}  {op['name']}  {op['mode']}  [bold]{op['state']}[/bold]"
                      f"  {op['args_digest']}")
    if "result" in item and item.get("result") is not None:
        console.print("  result: " + json.dumps(item["result"], default=str)[:2000])


def _read_input(value: Optional[str]):
    if value is None:
        return None
    text = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except ValueError:
        raise typer.BadParameter("--input must be a JSON file (or - for standard input)")


@work_items_app.command("validate")
def validate(hub: Path = typer.Argument(Path("."), help="Hub folder."),
             as_json: bool = typer.Option(False, "--json")) -> None:
    """Check work-items.yaml and every workflow's routes, without running anything."""
    from ..workflows.runtime import load_definitions
    from . import config

    hub = _hub(hub)
    cfg = _run(config.load, as_json, hub)
    workflows = {w.name: w.extra.get("work_items") for w in load_definitions(hub)
                 if (w.extra or {}).get("work_items")}
    out = {"ok": True, "config": cfg.to_dict(), "present": cfg.present, "workflows": workflows}

    def render(v):
        console.print(f"[green]ok[/green] {'work-items.yaml' if v['present'] else 'no work-items.yaml (defaults)'}")
        for action, mode in v["config"]["write_modes"].items():
            console.print(f"  {action}: {mode}")
        for name, routes in v["workflows"].items():
            console.print(f"  workflow {name}: routes {', '.join(routes)}")
        if not v["workflows"]:
            console.print("  no workflow declares work_items yet")

    _out(out, as_json, render)


@work_items_app.command("start")
def start(hub: Path = typer.Argument(Path("."), help="Hub folder."),
          workflow: str = typer.Option(..., "--workflow", help="The workflow function name."),
          key: str = typer.Option(..., "--key", help="A stable id, such as operator:demo-1."),
          route: Optional[str] = typer.Option(None, "--route"),
          input_file: Optional[str] = typer.Option(None, "--input", help="A JSON file, or - for stdin."),
          new_generation: bool = typer.Option(False, "--new-generation",
                                              help="Start new work for a key whose work has finished."),
          as_: Optional[str] = typer.Option(None, "--as", help="Who asked for the work."),
          as_json: bool = typer.Option(False, "--json")) -> None:
    """Start durable work. The same key and input returns the existing item."""
    from . import service

    hub = _hub(hub)
    requester = _person(hub, as_)
    item, created = _run(service.admit_and_start, as_json, hub, workflow=workflow, key=key, route=route,
                         input=_read_input(input_file), requester=requester, actor=_operator(),
                         generation_mode="new" if new_generation else "reuse",
                         origin={"source": "cli", "surface": "cli"})
    shown = _run(service.show, as_json, hub, item["id"], _operator())
    shown["created"] = created

    def render(v):
        console.print(("Started " if v["created"] else "Already started: ") + _item_line(v))
        console.print(f"Watch it with: hubzoid work-items show {hub} {v['id']}")

    _out(shown, as_json, render)


@work_items_app.command("list")
def list_cmd(hub: Path = typer.Argument(Path(".")), state: Optional[str] = typer.Option(None, "--state"),
             workflow: Optional[str] = typer.Option(None, "--workflow"),
             limit: int = typer.Option(50, "--limit"), as_json: bool = typer.Option(False, "--json")) -> None:
    """List work items, newest first."""
    from . import service

    hub = _hub(hub)
    out = _run(service.list_items, as_json, hub, _operator(), state=state, workflow=workflow, limit=limit)

    def render(v):
        if not v["items"]:
            console.print("No work items yet.")
        for item in v["items"]:
            console.print(_item_line(item))

    _out(out, as_json, render)


@work_items_app.command("show")
def show(hub: Path = typer.Argument(Path(".")), item_id: str = typer.Argument(...),
         as_json: bool = typer.Option(False, "--json")) -> None:
    """One work item: stages, operations, state and result."""
    from . import service

    hub = _hub(hub)
    item = _run(service.show, as_json, hub, item_id, _operator())
    _out(item, as_json, _render_item, waiting=item["state"] in ("waiting", "needs_input"))


@work_items_app.command("message")
def message(hub: Path = typer.Argument(Path(".")), item_id: str = typer.Argument(...),
            text: str = typer.Option(..., "--text"),
            expected_revision: Optional[int] = typer.Option(None, "--expected-revision"),
            as_json: bool = typer.Option(False, "--json")) -> None:
    """Send a follow-up to active work. It is read at the next stage boundary."""
    from . import service

    hub = _hub(hub)
    out = _run(service.message, as_json, hub, item_id, _operator(), text,
               expected_revision=expected_revision)
    _out(out, as_json, lambda v: console.print(f"Queued follow-up {v['message_id']}."))


@work_items_app.command("cancel")
def cancel(hub: Path = typer.Argument(Path(".")), item_id: str = typer.Argument(...),
           reason: str = typer.Option("", "--reason"),
           expected_revision: Optional[int] = typer.Option(None, "--expected-revision"),
           as_json: bool = typer.Option(False, "--json")) -> None:
    """Cancel work. Partial output and the record of what it did stay."""
    from . import service

    hub = _hub(hub)
    out = _run(service.cancel, as_json, hub, item_id, _operator(), reason=reason, surface="cli",
               expected_revision=expected_revision)
    _out(out, as_json, lambda v: console.print(f"Cancelled {v['id']}."))


@work_items_app.command("resume")
def resume(hub: Path = typer.Argument(Path(".")), item_id: str = typer.Argument(...),
           expected_revision: Optional[int] = typer.Option(None, "--expected-revision"),
           as_json: bool = typer.Option(False, "--json")) -> None:
    """Continue waiting work whose decisions are all made."""
    from . import service

    hub = _hub(hub)
    out = _run(service.resume, as_json, hub, item_id, _operator(), expected_revision=expected_revision,
               surface="cli")
    _out(out, as_json, lambda v: console.print(f"Continuing {v['id']} as execution {v['execution']}."))


@work_items_app.command("restart")
def restart(hub: Path = typer.Argument(Path(".")), item_id: str = typer.Argument(...),
            stage: Optional[str] = typer.Option(None, "--stage",
                                                help="Re-run this saved stage and everything after it."),
            reason: str = typer.Option(..., "--reason"),
            expected_revision: Optional[int] = typer.Option(None, "--expected-revision"),
            as_json: bool = typer.Option(False, "--json")) -> None:
    """Restart stopped work on the current code, reusing earlier saved stages."""
    from . import service

    hub = _hub(hub)
    out = _run(service.restart, as_json, hub, item_id, _operator(), stage=stage, reason=reason,
               expected_revision=expected_revision, surface="cli")
    _out(out, as_json, lambda v: console.print(f"Restarted {v['id']} as execution {v['execution']}."))


@work_items_app.command("config")
def config_show(hub: Path = typer.Argument(Path(".")), as_json: bool = typer.Option(False, "--json")) -> None:
    """The effective work-items.yaml."""
    from . import config

    hub = _hub(hub)
    cfg = _run(config.load, as_json, hub)
    _out({"revision": cfg.digest or None, "effective": cfg.to_dict(),
          "sources": [config.FILE] if cfg.present else []}, as_json)


@operations_app.command("list")
def ops_list(hub: Path = typer.Argument(Path(".")), state: Optional[str] = typer.Option(None, "--state"),
             as_json: bool = typer.Option(False, "--json")) -> None:
    """Recorded external writes, newest first. `--state ambiguous` lists what needs a decision."""
    from . import service

    hub = _hub(hub)
    out = _run(service.list_operations, as_json, hub, _operator(), state=state)

    def render(v):
        if not v["operations"]:
            console.print("No operations.")
        for op in v["operations"]:
            console.print(f"{op['id']}  {op['name']}  {op['mode']}  {op['state']}  item {op['item_id']}  "
                          f"{op['args_digest']}")

    _out(out, as_json, render)


@operations_app.command("show")
def ops_show(hub: Path = typer.Argument(Path(".")), op_id: str = typer.Argument(...),
             as_json: bool = typer.Option(False, "--json")) -> None:
    """One operation, with the exact content a person approves."""
    from . import service

    hub = _hub(hub)
    _out(_run(service.operation_detail, as_json, hub, op_id, _operator()), as_json)


@operations_app.command("approve")
def ops_approve(hub: Path = typer.Argument(Path(".")), op_id: str = typer.Argument(...),
                digest: str = typer.Option(..., "--digest", help="The digest you reviewed (sha256:...)."),
                expected_revision: Optional[int] = typer.Option(None, "--expected-revision"),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """Approve the exact content of a waiting operation. The work continues and sends it once."""
    from . import service

    hub = _hub(hub)
    out = _run(service.approve, as_json, hub, op_id, _decider(hub, as_json), digest=digest,
               expected_revision=expected_revision, surface="cli")
    _out(out, as_json, lambda v: console.print(f"Approved. Work item is {(v['work_item'] or {}).get('state')}."))


@operations_app.command("deny")
def ops_deny(hub: Path = typer.Argument(Path(".")), op_id: str = typer.Argument(...),
             reason: str = typer.Option("", "--reason"),
             expected_revision: Optional[int] = typer.Option(None, "--expected-revision"),
             as_json: bool = typer.Option(False, "--json")) -> None:
    """Deny a waiting operation. The work stops; nothing is sent."""
    from . import service

    hub = _hub(hub)
    out = _run(service.deny, as_json, hub, op_id, _decider(hub, as_json), reason=reason,
               expected_revision=expected_revision, surface="cli")
    _out(out, as_json, lambda v: console.print("Denied. Nothing was sent."))


@operations_app.command("resolve")
def ops_resolve(hub: Path = typer.Argument(Path(".")), op_id: str = typer.Argument(...),
                decision: str = typer.Option(..., "--decision", help="done, retry or abandon."),
                evidence: Optional[str] = typer.Option(None, "--evidence",
                                                       help="Where you confirmed it: text, or a file."),
                acknowledge_duplicate_risk: bool = typer.Option(False, "--acknowledge-duplicate-risk"),
                expected_revision: Optional[int] = typer.Option(None, "--expected-revision"),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """Decide an operation whose outcome is unknown: done, retry or abandon."""
    from . import service

    hub = _hub(hub)
    proof = evidence
    if evidence and Path(evidence).is_file():
        proof = Path(evidence).read_text(encoding="utf-8")
    out = _run(service.resolve, as_json, hub, op_id, _decider(hub, as_json), decision=decision, evidence=proof,
               acknowledge_duplicate_risk=acknowledge_duplicate_risk,
               expected_revision=expected_revision, surface="cli")
    _out(out, as_json, lambda v: console.print(f"Resolved as {decision}. Operation is {v['operation']['state']}."))
