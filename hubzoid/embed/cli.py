"""``hubzoid embed``: apps that embed the chat (``apps:`` in hubzoid.yaml).

  hubzoid embed secret erpnext --origin https://erp.acme.com
                                   make the app's client secret (stored, shown once),
                                   with the hubzoid.yaml entry and server settings
  hubzoid embed list               the registered apps and whether they are ready
  hubzoid embed pass erpnext alice a short test pass, to try the chat by hand

The apps themselves live in hubzoid.yaml, like the other file-owned settings:
the command prints the entry, you (or Claude Code or Codex) add it to the file,
and every agent picks it up within a few seconds. The CLI never edits the file.
"""
from __future__ import annotations

import getpass
import json
import secrets
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()
embed_app = typer.Typer(help="Apps that embed the chat: secrets, status and test passes.", no_args_is_help=True)

_HUB = typer.Option(Path("."), "--hub", help="The hub folder (default: here).")


def _hub(hub: Path) -> Path:
    if not (hub / "AGENTS.md").is_file():
        console.print(f"[red]{escape(str(hub))} is not a hub folder (no AGENTS.md).[/red]")
        raise typer.Exit(2)
    return hub.resolve()


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001 - no login name on this machine
        return "cli"


@embed_app.command("secret")
def secret_cmd(app_id: str = typer.Argument(..., help="The app's client id, e.g. erpnext."),
               hub: Path = _HUB,
               origins: list[str] = typer.Option(None, "--origin", help="The app's web address (repeat)."),
               agents: list[str] = typer.Option(None, "--agent", help="An agent the app may use (repeat)."),
               tools: list[str] = typer.Option(None, "--tool", help="A tool the app may use (repeat; default all)."),
               token_to: list[str] = typer.Option(None, "--token-to",
                                                  help="A tool that gets the person's app token (repeat)."),
               rotate: bool = typer.Option(False, "--rotate", help="Replace an existing secret.")) -> None:
    """Make the app's client secret, store it (write-only) and show it once, with
    the hubzoid.yaml entry to paste and the settings for the app's server."""
    from ..secretstore import store
    from .config import ID_RE

    hub_dir = _hub(hub)
    if not ID_RE.match(app_id):
        console.print("[red]An app id is 2 to 40 lowercase letters, digits or _, starting with a letter.[/red]")
        raise typer.Exit(2)
    name = f"app-{app_id}"
    value = secrets.token_urlsafe(36)
    try:
        store.put(hub_dir, scope="deployment", name=name, value=value, actor=_actor(),
                  expected_revision=None if rotate else "absent", surface="cli")
    except store.SecretError as exc:
        if exc.status == 409:
            console.print(f"[red]{app_id} already has a secret. Use --rotate to replace it (the app must "
                          "then use the new one).[/red]")
            raise typer.Exit(4)
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(5)
    agent_list = [a.strip().lower() for a in (agents or [hub_dir.name])]

    def flow(items: list[str]) -> str:
        return "[" + ", ".join(json.dumps(i) if not i.replace("_", "").isalnum() else i for i in items) + "]"

    entry = [f"  {app_id}:", f"    secret_ref: secret:deployment/{name}",
             f"    origins: {flow(origins or ['https://your-app.example.com'])}",
             "    access:", f"      agents: {flow(agent_list)}", f"      tools: {flow(tools or ['*'])}"]
    if token_to:
        entry.append(f"    token_to: {flow(token_to)}")
    console.print(f"Client id: [bold]{app_id}[/bold]")
    console.print("Client secret (shown once):")
    print(value)
    if rotate:
        console.print("\nThe old secret stops working now: put the new one in the app's server.")
        return
    console.print("\n1. Add this under apps: in hubzoid.yaml (the agents pick it up within seconds):\n")
    print("\n".join(entry))
    console.print("\n2. Give the app's server these settings:\n")
    print(f"HUBZOID_URL={_agent_url(hub_dir, agent_list[0])}")
    print(f"HUBZOID_CLIENT_ID={app_id}")
    print("HUBZOID_CLIENT_SECRET=<the secret above>")
    console.print("\n3. Check it: hubzoid embed list. The rest is in docs/embedding.md.")


def _agent_url(hub_dir: Path, agent: str) -> str:
    """Where the app reaches this agent: the public address, then /b/<agent>
    behind a gateway of several agents."""
    from .. import deployfile

    try:
        found = deployfile.owning(hub_dir)
        df = deployfile.load(found) if found else None
    except Exception:  # noqa: BLE001 - only a hint for the person setting it up
        df = None
    base = ((df.public_url if df else None) or "https://<your Hubzoid address>").rstrip("/")
    return f"{base}/b/{agent}" if df is not None and df.hubs else base


@embed_app.command("list")
def list_cmd(hub: Path = _HUB, as_json: bool = typer.Option(False, "--json")) -> None:
    """The registered apps and whether each one is ready."""
    from .config import AppsError, service_for

    hub_dir = _hub(hub)
    svc = service_for(hub_dir)
    try:
        apps = svc.all()
    except AppsError as exc:
        console.print(f"[red]{escape(exc.message)}[/red]")
        raise typer.Exit(5)
    rows = []
    for app in apps.values():
        ready, why = True, ""
        if app.secret_ref:
            from ..secretstore import resolve

            try:
                resolve(app.secret_ref, hub_dir)
            except Exception:  # noqa: BLE001 - reported, not raised
                ready, why = False, f"no secret at {app.secret_ref}"
        rows.append({"id": app.id, "mode": app.mode, "enabled": app.enabled, "origins": list(app.origins),
                     "agents": sorted(app.maximum.agents), "tools": sorted(app.maximum.tools),
                     "token_to": list(app.token_to), "ready": ready and app.enabled, "problem": why})
    if as_json:
        print(json.dumps({"apps": rows, "file": svc.state()}))
        return
    if not rows:
        console.print("No apps yet. Add one under apps: in hubzoid.yaml (docs/embedding.md).")
        return
    table = Table("App", "Mode", "Agents", "Tools", "Origins", "State")
    for r in rows:
        state = "ready" if r["ready"] else ("off" if not r["enabled"] else r["problem"])
        table.add_row(r["id"], r["mode"], ", ".join(r["agents"]), ", ".join(r["tools"]),
                      ", ".join(r["origins"]) or "-", state)
    console.print(table)


@embed_app.command("pass")
def pass_cmd(app_id: str = typer.Argument(...), user: str = typer.Argument(..., help="The app's user id."),
             hub: Path = _HUB, minutes: int = typer.Option(10, min=1, max=60),
             tools: list[str] = typer.Option(None, "--tool", help="Narrow to these tools (repeat)."),
             app_token: str = typer.Option(None, "--app-token", help="The person's app token for the tools.")) -> None:
    """A short pass for a pass-mode app, signed with its secret: to try the chat by hand."""
    from joserfc import jwt
    from joserfc.jwk import OctKey

    from ..secretstore import resolve
    from .config import service_for

    hub_dir = _hub(hub)
    app = service_for(hub_dir).get(app_id)
    if app is None or app.mode != "pass":
        console.print(f"[red]{escape(app_id)} is not a pass-mode app in hubzoid.yaml.[/red]")
        raise typer.Exit(1)
    now = int(time.time())
    claims = {"iss": app.issuer, "aud": app.audience, "sub": user, "name": user, "iat": now,
              "exp": now + min(minutes * 60, app.max_lifetime)}
    if tools:
        claims["access"] = {"tools": list(tools)}
    if app_token:
        claims["app_token"] = app_token
    print(jwt.encode({"alg": "HS256"}, claims, OctKey.import_key(resolve(app.secret_ref, hub_dir).encode())))
