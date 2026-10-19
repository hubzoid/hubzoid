"""``hubzoid intake``: review messages from outside the organization.

    hubzoid intake list    [HUB] [--state quarantined] [--channel ID]
    hubzoid intake show    [HUB] ITEM
    hubzoid intake release [HUB] ITEM --reason TEXT --expected-revision N
    hubzoid intake reject  [HUB] ITEM --reason TEXT --expected-revision N
    hubzoid intake retry   [HUB] ITEM --expected-revision N
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()
intake_app = typer.Typer(help="Intake: review email and WhatsApp messages before work starts.",
                         no_args_is_help=True)
HUB = typer.Argument(Path("."), help="The hub folder.")
ITEM = typer.Argument(..., metavar="ITEM")


def _table(data):
    t = Table("id", "state", "reason", "from", "subject")
    for i in data["items"]:
        t.add_row(i["id"][:8], i["state"], i["reason"] or "", escape(i["sender"].get("external_id") or ""),
                  escape(i["subject"] or i["summary"] or ""))
    console.print(t)


@intake_app.command("list")
def list_cmd(hub: Path = HUB, state: str = typer.Option(None, "--state"), channel: str = typer.Option(None, "--channel"),
             as_json: bool = typer.Option(False, "--json")):
    """Intake items, oldest first."""
    from ..channels.cli import _hub, _out, _run
    from . import service

    hub = _hub(hub, as_json)
    _out(_run(service.list_items, as_json, hub, channel=channel, state=state), as_json, _table)


def _find(hub, item, as_json):
    from ..channels.cli import _fail
    from . import service

    if len(item) >= 32:
        return item
    hits = [i["id"] for i in service.list_items(hub, limit=100)["items"] if i["id"].startswith(item)]
    if len(hits) != 1:
        _fail("not_found", f"No single intake item starts with {item}.", 404, as_json)
    return hits[0]


@intake_app.command("show")
def show_cmd(hub: Path = HUB, item: str = ITEM, as_json: bool = typer.Option(False, "--json")):
    """One item with its text and attachments (untrusted data)."""
    from ..channels.cli import _hub, _out, _run
    from . import service

    hub = _hub(hub, as_json)
    row = _run(service.require, as_json, hub, _find(hub, item, as_json))
    _out(service.view(row, full=True, hub_dir=hub), as_json)


def _decide(fn, hub, item, as_json, **kw):
    from ..channels.cli import _hub, _out, _run
    from . import service

    hub = _hub(hub, as_json)
    out = _run(fn, as_json, hub, _find(hub, item, as_json), actor="cli", **kw)
    _out(service.view(out), as_json, lambda d: console.print(f"{d['id'][:8]} is now {d['state']}."
                                                             + (f" Work item {d['work_item_id'][:8]}."
                                                                if d.get("work_item_id") else "")))


@intake_app.command("release")
def release_cmd(hub: Path = HUB, item: str = ITEM, reason: str = typer.Option(..., "--reason"),
                expected_revision: int = typer.Option(..., "--expected-revision"),
                as_json: bool = typer.Option(False, "--json")):
    """Start work on a quarantined item. This approves nothing it later wants to send."""
    from . import service

    _decide(service.release, hub, item, as_json, reason=reason, expected_revision=expected_revision)


@intake_app.command("reject")
def reject_cmd(hub: Path = HUB, item: str = ITEM, reason: str = typer.Option("", "--reason"),
               expected_revision: int = typer.Option(..., "--expected-revision"),
               as_json: bool = typer.Option(False, "--json")):
    """Close an item without starting work. Nothing is sent to the sender."""
    from . import service

    _decide(service.reject, hub, item, as_json, reason=reason, expected_revision=expected_revision)


@intake_app.command("retry")
def retry_cmd(hub: Path = HUB, item: str = ITEM, expected_revision: int = typer.Option(..., "--expected-revision"),
              as_json: bool = typer.Option(False, "--json")):
    """Fetch a failed item's attachments again, then decide again."""
    from . import service

    _decide(service.retry, hub, item, as_json, expected_revision=expected_revision)
