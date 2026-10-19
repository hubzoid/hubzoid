"""``hubzoid channels email``: connect a mailbox, read it once, check drafts.

    hubzoid channels email status         [HUB] ID
    hubzoid channels email connect        [HUB] ID [--mode approve|draft]
    hubzoid channels email poll           [HUB] ID
    hubzoid channels email rehearse-draft [HUB] ID
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

console = Console()
email_app = typer.Typer(help="Email: connect a shared mailbox and check how replies are handled.",
                        no_args_is_help=True)
HUB = typer.Argument(Path("."), help="The hub folder.")
ID = typer.Argument(..., metavar="ID")


def _conn(hub, cid, as_json):
    from .. import config
    from ..cli import _fail, _hub, _run

    hub = _hub(hub, as_json)
    conn = _run(config.active, as_json, hub, cid)
    if conn.provider != "email":
        _fail("not_found", f"{cid} is not an email connection.", 404, as_json)
    return hub, conn


@email_app.command("status")
def status_cmd(hub: Path = HUB, cid: str = ID, as_json: bool = typer.Option(False, "--json")):
    """Whether the mailbox is connected, when it was last read and how replies are handled."""
    from ..cli import _out, _run
    from . import status

    _, conn = _conn(hub, cid, as_json)
    _out(_run(status.status, as_json, conn), as_json)


@email_app.command("connect")
def connect_cmd(hub: Path = HUB, cid: str = ID,
                mode: str = typer.Option("approve", "--mode", help="approve (send after approval) or draft"),
                as_json: bool = typer.Option(False, "--json")):
    """A consent link for Gmail or Microsoft 365. Open it signed in as the mailbox."""
    from ..cli import _out, _run
    from . import oauth

    _, conn = _conn(hub, cid, as_json)
    out = _run(oauth.connect, as_json, conn, actor="cli", mode=mode)
    _out(out, as_json, lambda d: console.print(
        f"Open this link signed in as [b]{escape(conn.settings.mailbox)}[/b] (valid {d['expires_in'] // 60} "
        f"minutes):\n{escape(d['authorization_url'])}"))


@email_app.command("poll")
def poll_cmd(hub: Path = HUB, cid: str = ID, as_json: bool = typer.Option(False, "--json")):
    """Read new mail once now. The first run only records where to start."""
    from ..cli import _out, _run
    from .poller import poll_once

    _, conn = _conn(hub, cid, as_json)
    out = _run(poll_once, as_json, conn, owner="cli")
    _out(out, as_json, lambda d: console.print(
        "Started: new mail from now on will be read." if d["state"] == "started" else
        f"{d['state']}: {d['messages']} new message(s)."))


@email_app.command("rehearse-draft")
def rehearse_cmd(hub: Path = HUB, cid: str = ID, as_json: bool = typer.Option(False, "--json")):
    """Save one real draft (to the mailbox itself) and read it back. Nothing is sent."""
    from ..cli import _out, _run
    from . import status

    _, conn = _conn(hub, cid, as_json)
    out = _run(status.rehearse_draft, as_json, conn, actor="cli")
    _out(out, as_json, lambda d: console.print(f"[green]Draft saved and read back[/green] ({escape(d['draft_id'])})."
                                               " Nothing was sent. email.send: draft can now be used."))
