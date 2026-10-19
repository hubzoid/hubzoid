"""``hubzoid channels github``: set up and use a GitHub connection.

    hubzoid channels github connect      [HUB] ID --owner LOGIN --owner-type organization|user --name NAME [--open]
    hubzoid channels github connect      [HUB] ID --mode pat --repository OWNER/NAME [--token-stdin]
    hubzoid channels github setup status [HUB] ID
    hubzoid channels github setup verify [HUB] ID --installation-id N
    hubzoid channels github setup cancel [HUB] ID
    hubzoid channels github import-app   [HUB] ID --app-id N --installation-id N --owner-id N
    hubzoid channels github repositories [HUB] ID
    hubzoid channels github configure    [HUB] ID --expected-revision N [--repository OWNER/NAME]... [--set KEY=JSON]...
    hubzoid channels github intake       [HUB] ID --url ISSUE_URL --task TEXT
    hubzoid channels github call         [HUB] ID --operation NAME --arguments-file FILE

A token is read hidden from the terminal, or from standard input with
--token-stdin, never from an argument, and never printed.
"""
from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

console = Console()
github_app = typer.Typer(help="GitHub: the customer's own App or a local token, repositories and intake.",
                         no_args_is_help=True)
setup_app = typer.Typer(help="The App setup in progress.", no_args_is_help=True)
github_app.add_typer(setup_app, name="setup")

HUB = typer.Argument(Path("."), help="The hub folder.")
ID = typer.Argument(..., metavar="ID")
JSON_OPT = typer.Option(False, "--json")


def _conn(hub, cid, as_json):
    from .. import config
    from ..cli import _fail, _hub, _run

    hub = _hub(hub, as_json)
    conn = _run(config.active, as_json, hub, cid)
    if conn.provider != "github":
        _fail("not_found", f"{cid} is not a GitHub connection.", 404, as_json)
    return hub, conn


def _print(data, as_json, line=None):
    if as_json or line is None:
        print(json.dumps(data, default=str, indent=None if as_json else 2))
    else:
        console.print(line)


@github_app.command("connect")
def connect_cmd(hub: Path = HUB, cid: str = ID,
                owner: str = typer.Option(None, "--owner", help="The GitHub account or organization that owns the App."),
                owner_type: str = typer.Option("organization", "--owner-type", help="organization or user"),
                name: str = typer.Option(None, "--name", help="The App's name on GitHub."),
                mode: str = typer.Option("app", "--mode", help="app (default) or pat for a local hub"),
                repository: list[str] = typer.Option([], "--repository", help="OWNER/NAME, for --mode pat"),
                token_stdin: bool = typer.Option(False, "--token-stdin", help="Read the token from standard input."),
                open_: bool = typer.Option(False, "--open", help="Open the registration page in a browser."),
                as_json: bool = JSON_OPT):
    """Start the App setup (prints the page that creates the App on GitHub), or connect a local token."""
    from ..cli import _actor, _fail, _hub, _run
    from . import setup

    if mode == "pat":
        hub = _hub(hub, as_json)
        if not repository:
            _fail("validation_failed", "Name at least one --repository OWNER/NAME.", 422, as_json)
        token = sys.stdin.readline().strip() if token_stdin else getpass.getpass("GitHub token: ").strip()
        if not token:
            _fail("validation_failed", "No token given.", 422, as_json)
        from .. import config

        try:
            current = config.export(hub, cid)["revision"]
        except Exception:  # noqa: BLE001 - a new connection
            current = "absent"
        out = _run(setup.connect_pat, as_json, hub, cid, token=token, repositories_=repository, actor=_actor(),
                   expected_revision=current)
        _print(out, as_json, f"[green]{cid} uses your token (revision {out['revision']}).[/green] Replies use your "
                             "GitHub account. Run doctor, then start work with hubzoid channels github intake.")
        return
    if not owner:
        _fail("validation_failed", "Name the App's owner with --owner.", 422, as_json)
    hub, conn = _conn(hub, cid, as_json)
    out = _run(setup.start, as_json, conn, owner=owner, owner_type=owner_type, name=name or cid, actor=_actor())
    if open_:
        import webbrowser

        webbrowser.open(out["registration_url"])
    _print(out, as_json, f"Open this page to create the App on GitHub (it expires in 15 minutes):\n"
                         f"{escape(out['registration_url'])}\nThen install it on your repositories. Check progress "
                         f"with hubzoid channels github setup status {hub} {cid}.")


@setup_app.command("status")
def setup_status_cmd(hub: Path = HUB, cid: str = ID, as_json: bool = JSON_OPT):
    """Where the App setup is: awaiting_registration, awaiting_installation or ready."""
    from . import setup

    hub, conn = _conn(hub, cid, as_json)
    out = setup.status(conn)
    _print(out, as_json, f"Setup: {out['state']}" + (f". Install it: {out['installation_url']}"
                                                     if out.get("installation_url") else ""))


@setup_app.command("verify")
def setup_verify_cmd(hub: Path = HUB, cid: str = ID, installation_id: int = typer.Option(..., "--installation-id"),
                     as_json: bool = JSON_OPT):
    """Check an installation with the App's own credentials and save it."""
    from ..cli import _actor, _run
    from . import setup

    hub, conn = _conn(hub, cid, as_json)
    out = _run(setup.installed, as_json, conn, installation_id, None, actor=_actor())
    _print(out, as_json, f"[green]Installation {out['installation_id']} on {out['owner']} verified "
                         f"(revision {out['revision']}).[/green]")


@setup_app.command("cancel")
def setup_cancel_cmd(hub: Path = HUB, cid: str = ID, as_json: bool = JSON_OPT):
    """Forget the setup in progress. An App already created stays on GitHub."""
    from ..cli import _actor
    from . import setup

    hub, conn = _conn(hub, cid, as_json)
    setup.cancel(conn, _actor())
    _print({"state": "none"}, as_json, "Setup cancelled.")


@github_app.command("import-app")
def import_app_cmd(hub: Path = HUB, cid: str = ID, app_id: int = typer.Option(..., "--app-id"),
                   installation_id: int = typer.Option(..., "--installation-id"),
                   owner_id: int = typer.Option(..., "--owner-id"), as_json: bool = JSON_OPT):
    """Use an App you already have. Store its private key and webhook secret first."""
    from ..cli import _actor, _run
    from . import setup

    hub, conn = _conn(hub, cid, as_json)
    out = _run(setup.import_app, as_json, conn, app_id=app_id, installation_id=installation_id, owner_id=owner_id,
               actor=_actor())
    _print(out, as_json, f"[green]App {app_id} imported (revision {out['revision']}).[/green]")


@github_app.command("repositories")
def repositories_cmd(hub: Path = HUB, cid: str = ID, as_json: bool = JSON_OPT):
    """The repositories the installation (or token) can reach."""
    from ..cli import _run
    from . import setup

    hub, conn = _conn(hub, cid, as_json)
    items = _run(setup.repositories, as_json, conn)
    _print({"items": items}, as_json, "\n".join(f"{r['id']}  {r['full_name']}" for r in items) or "None yet.")


@github_app.command("configure")
def configure_cmd(hub: Path = HUB, cid: str = ID, expected_revision: int = typer.Option(..., "--expected-revision"),
                  repository: list[str] = typer.Option([], "--repository", help="OWNER/NAME, resolved to its id"),
                  sets: list[str] = typer.Option([], "--set", help="KEY=JSON, for example enabled=true"),
                  as_json: bool = JSON_OPT):
    """Choose repositories by name (saved by id) and change settings, as one new revision."""
    from ..cli import _actor, _fail, _run
    from . import setup

    hub, conn = _conn(hub, cid, as_json)
    values = {}
    for item in sets:
        key, sep, raw = item.partition("=")
        if not sep:
            _fail("validation_failed", f"--set {item!r} needs KEY=VALUE.", 422, as_json)
        try:
            values[key.strip()] = json.loads(raw)
        except ValueError:
            values[key.strip()] = raw
    out = _run(setup.configure, as_json, conn, expected_revision=expected_revision, actor=_actor(),
               repositories_=repository or None, sets=values)
    _print(out, as_json, f"[green]{cid}: revision {out['revision']}, {out['status']}.[/green]")


@github_app.command("intake")
def intake_cmd(hub: Path = HUB, cid: str = ID, url: str = typer.Option(..., "--url"),
               task: str = typer.Option(..., "--task"), as_json: bool = JSON_OPT):
    """Local token mode: start work on an issue, as the token's owner."""
    from ..cli import _run
    from . import setup

    hub, conn = _conn(hub, cid, as_json)
    out = _run(setup.manual_intake, as_json, conn, url=url, task=task)
    _print(out, as_json, f"Accepted as delivery {out['delivery_id'][:8]}. A running hub starts the work.")


@github_app.command("call")
def call_cmd(hub: Path = HUB, cid: str = ID, operation: str = typer.Option(..., "--operation"),
             arguments_file: Path = typer.Option(..., "--arguments-file"), as_json: bool = JSON_OPT):
    """Run one GitHub operation, as an agent would. Writes run only inside channel work."""
    from ..cli import _run
    from . import operations

    hub, conn = _conn(hub, cid, as_json)
    args = json.loads(arguments_file.read_text(encoding="utf-8"))
    out = _run(operations.call, as_json, conn, operation, args)
    _print(out, as_json, None)
