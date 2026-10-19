"""``hubzoid channels linear``: install the hub's Linear app and look at its sessions.

    hubzoid channels linear setup      [HUB] ID
    hubzoid channels linear connect    [HUB] ID [--wait SECONDS]
    hubzoid channels linear disconnect [HUB] ID
    hubzoid channels linear sessions   [HUB] ID
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

console = Console()
linear_app = typer.Typer(help="Linear: install the hub's agent app and see its sessions.", no_args_is_help=True)
HUB = typer.Argument(Path("."), help="The hub folder.")
ID = typer.Argument(..., metavar="ID")


def _conn(hub, cid, as_json):
    from .. import config
    from ..cli import _fail, _hub, _run

    hub = _hub(hub, as_json)
    conn = _run(config.active, as_json, hub, cid)
    if conn.provider != "linear":
        _fail("not_found", f"{cid} is not a Linear connection.", 404, as_json)
    return hub, conn


@linear_app.command("setup")
def setup_cmd(hub: Path = HUB, cid: str = ID, as_json: bool = typer.Option(False, "--json")):
    """The exact values to enter when creating the OAuth app in Linear."""
    from ..cli import _run
    from . import oauth

    hub, conn = _conn(hub, cid, as_json)
    out = _run(oauth.setup_checklist, as_json, conn)
    if as_json:
        print(json.dumps(out))
        return
    for n, step in enumerate(out["steps"], 1):
        console.print(f"{n}. {escape(step)}")
    console.print(f"Scopes: {', '.join(out['scopes'])}. Installed: {'yes' if out['installed'] else 'not yet'}.")


@linear_app.command("connect")
def connect_cmd(hub: Path = HUB, cid: str = ID, wait: int = typer.Option(0, "--wait", help="Seconds to wait for it."),
                as_json: bool = typer.Option(False, "--json")):
    """Print the approval link for a workspace admin, and optionally wait until it is used."""
    from .. import config
    from ..cli import _actor, _run
    from . import oauth

    hub, conn = _conn(hub, cid, as_json)
    out = _run(oauth.connect, as_json, conn, actor=_actor())
    if as_json:
        print(json.dumps(out))
    else:
        console.print(f"A Linear workspace admin opens this link and approves the app (valid 10 minutes):\n"
                      f"{escape(out['authorization_url'])}")
    deadline = time.time() + max(0, wait)
    while time.time() < deadline:
        time.sleep(2)
        state = config.active(hub, cid).state or {}
        if state.get("linear_app_user_id") and not state.get("linear_blocked"):
            console.print(f"[green]Connected to {escape(state.get('linear_org_name') or 'Linear')}.[/green]")
            return
    if wait:
        console.print("[yellow]Not approved yet. Run doctor after the admin approves.[/yellow]")


@linear_app.command("disconnect")
def disconnect_cmd(hub: Path = HUB, cid: str = ID, as_json: bool = typer.Option(False, "--json")):
    """Revoke the app's token here. New sessions and replies stop until you reconnect."""
    from ..cli import _actor, _run
    from . import oauth

    hub, conn = _conn(hub, cid, as_json)
    out = _run(oauth.disconnect, as_json, conn, actor=_actor())
    print(json.dumps(out)) if as_json else console.print("Disconnected.")


@linear_app.command("sessions")
def sessions_cmd(hub: Path = HUB, cid: str = ID, as_json: bool = typer.Option(False, "--json")):
    """Agent sessions this connection has worked in."""
    from .. import store

    hub, conn = _conn(hub, cid, as_json)
    out = store.list_threads(hub, conn.hub, cid)
    items = [{"thread_id": t["id"], "agent_session_id": t["origin_json"]["coordinates"].get("agent_session_id"),
              "url": t["origin_json"].get("url"), "requester": t["requester"]} for t in out["items"]]
    if as_json:
        print(json.dumps({"items": items}))
    else:
        for i in items:
            console.print(f"{i['agent_session_id']}  {i['requester']}  {i['url']}")
