"""``hubzoid channels clickup``: point ClickUp at the hub, and find people to link.

    hubzoid channels clickup setup    [HUB] ID     one webhook per list, secrets kept in the channel's slot
    hubzoid channels clickup members  [HUB] ID     the workspace's people, for `hubzoid channels link`
    hubzoid channels clickup teardown [HUB] ID     delete the webhooks this connection made
"""
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

console = Console()
clickup_app = typer.Typer(help="ClickUp: webhooks for the allowed lists, and people to link.", no_args_is_help=True)
HUB = typer.Argument(Path("."), help="The hub folder.")
ID = typer.Argument(..., metavar="ID")


def _conn(hub, cid, as_json):
    from .. import config
    from ..cli import _fail, _hub, _run

    hub = _hub(hub, as_json)
    conn = _run(config.active, as_json, hub, cid)
    if conn.provider != "clickup":
        _fail("not_found", f"{cid} is not a ClickUp connection.", 404, as_json)
    return hub, conn


@clickup_app.command("setup")
def setup_cmd(hub: Path = HUB, cid: str = ID, as_json: bool = typer.Option(False, "--json")):
    """Create (or repair) one webhook per allowed list, pointed at this hub."""
    from ..cli import _actor, _run
    from .adapter import setup

    hub, conn = _conn(hub, cid, as_json)
    out = _run(setup, as_json, conn, actor=_actor())
    if as_json:
        print(json.dumps(out))
        return
    console.print(f"Deliveries go to {escape(out['endpoint'])}.")
    if out["made"]:
        console.print(f"Made webhooks for lists {', '.join(out['made'])}.")
    if out["kept"]:
        console.print(f"Kept the webhooks for lists {', '.join(out['kept'])}.")
    if out["removed"]:
        console.print(f"Removed {len(out['removed'])} stale webhook(s).")
    console.print("Each webhook's secret is kept in this channel's secret slot. Next: link people with "
                  f"hubzoid channels link . {escape(cid)} --external-id <user id> --account <email>.")


@clickup_app.command("members")
def members_cmd(hub: Path = HUB, cid: str = ID, as_json: bool = typer.Option(False, "--json")):
    """The workspace's people and their ClickUp user ids."""
    from ..cli import _run
    from . import api

    hub, conn = _conn(hub, cid, as_json)

    def members():
        teams = (api.call(conn, "GET", "/team") or {}).get("teams") or []
        team = next((t for t in teams if str(t.get("id")) == conn.settings.workspace_id), None)
        return [{"id": str((m.get("user") or {}).get("id")), "name": (m.get("user") or {}).get("username"),
                 "email": (m.get("user") or {}).get("email")} for m in (team or {}).get("members") or []]

    out = _run(members, as_json)
    if as_json:
        print(json.dumps({"items": out}))
        return
    for m in out:
        console.print(f"{m['id']}  {escape(m['name'] or '')}  {escape(m['email'] or '')}")


@clickup_app.command("teardown")
def teardown_cmd(hub: Path = HUB, cid: str = ID, as_json: bool = typer.Option(False, "--json")):
    """Delete the webhooks this connection made. Tasks stop starting work."""
    from ..cli import _run
    from .adapter import teardown

    hub, conn = _conn(hub, cid, as_json)
    out = _run(teardown, as_json, conn)
    print(json.dumps(out)) if as_json else console.print(f"Deleted {len(out['deleted'])} webhook(s).")
