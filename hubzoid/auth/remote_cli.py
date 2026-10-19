"""``hubzoid auth`` and ``hubzoid api``: your account on a hosted Hubzoid.

    hubzoid auth login --server https://team.example.com
    hubzoid auth whoami
    hubzoid auth keys create "CI evals" --preset read --days 30
    hubzoid auth keys list
    hubzoid auth sessions list
    hubzoid auth sessions revoke-others
    hubzoid auth mfa status
    hubzoid api GET /api/agents
    HUBZOID_API_KEY=hzp_... hubzoid api POST /api/chat/... --data @body.json

``--server`` or ``HUBZOID_SERVER`` names the deployment; ``HUBZOID_API_KEY``
or the session from ``auth login`` signs the request (``control.remote``).
Passwords and codes are typed at a hidden prompt (or read from stdin with
``--password-stdin``), never passed as arguments, never printed.
"""
from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ..control import remote

auth_app = typer.Typer(help="Your account on a hosted Hubzoid: sign in, keys, devices, two-step sign-in.",
                       no_args_is_help=True)
keys_app = typer.Typer(help="Personal API keys.", no_args_is_help=True)
sessions_app = typer.Typer(help="Your signed-in devices.", no_args_is_help=True)
mfa_app = typer.Typer(help="Two-step sign-in.", no_args_is_help=True)
auth_app.add_typer(keys_app, name="keys")
auth_app.add_typer(sessions_app, name="sessions")
auth_app.add_typer(mfa_app, name="mfa")

console = Console()
err = Console(stderr=True)
_SERVER = typer.Option(None, "--server", help="The deployment's URL. Default: HUBZOID_SERVER.")
_JSON = typer.Option(False, "--json", help="Print the API's answer as JSON.")


def _client(server: str | None) -> remote.Client:
    try:
        url = remote.server_from(server)
    except remote.RemoteError as exc:
        _fail(exc)
    if not url:
        err.print("[red]Name the deployment: --server https://... or HUBZOID_SERVER.[/red]")
        raise typer.Exit(2)
    return remote.Client(url)


def _fail(exc: remote.RemoteError, as_json: bool = False):
    if as_json:
        print(json.dumps(exc.body if exc.body is not None else
                         {"detail": {"code": "error", "message": exc.message}}))
    else:
        err.print(f"[red]{exc.message}[/red]")
    raise typer.Exit(exc.code or 1)


def _run(server, method, path, body=None, as_json=False):
    c = _client(server)
    try:
        return c.call(method, path, body)
    except remote.RemoteError as exc:
        _fail(exc, as_json)


def _print(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))


@auth_app.command("login")
def login(server: str = _SERVER,
          email: str = typer.Option(None, "--email", help="Your email. Asked when not given."),
          password_stdin: bool = typer.Option(False, "--password-stdin",
                                              help="Read the password from standard input.")):
    """Sign in with your password (and a code, when you use two-step sign-in).
    The session is saved for this server; the password never is."""
    c = _client(server)
    if c.api_key:
        err.print("[yellow]HUBZOID_API_KEY is set; commands use it, not a saved session.[/yellow]")
    email = email or typer.prompt("Email")
    password = sys.stdin.readline().rstrip("\n") if password_stdin else getpass.getpass("Password: ")
    c.api_key = None
    try:
        r = c.request("POST", "/api/auth/login", {"email": email, "password": password})
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise remote.RemoteError(remote.exit_code(r.status_code),
                                     (data.get("detail") or {}).get("message") or f"HTTP {r.status_code}")
        jar = dict(r.cookies)
        if data.get("state") == "mfa_setup":
            err.print("[yellow]Your account must set up two-step sign-in first. Sign in once in "
                      "the browser to set it up, then run this again.[/yellow]")
            raise typer.Exit(3)
        if data.get("state") == "mfa_required":
            code = getpass.getpass("Code from your authenticator app (or a recovery code): ").strip()
            field = "code" if code.replace(" ", "").isdigit() else "recovery_code"
            r = c.request("POST", "/api/auth/mfa/verify", {field: code}, cookies=jar)
            data = r.json() if r.content else {}
            if r.status_code >= 400:
                raise remote.RemoteError(remote.exit_code(r.status_code),
                                         (data.get("detail") or {}).get("message") or f"HTTP {r.status_code}")
            jar = dict(r.cookies)
        token = jar.get(remote.SESSION_COOKIE)
        if not token:
            raise remote.RemoteError(1, "The server did not start a session.")
    except remote.RemoteError as exc:
        _fail(exc)
    remote.save_session(c.server, token, email)
    console.print(f"Signed in to {c.server} as {email}.")


@auth_app.command("bootstrap")
def bootstrap(hub: Path = typer.Argument(Path("."), help="The hub (any hub of a gateway)."),
              email: str = typer.Option(..., "--email", help="The owner's email."),
              name: str = typer.Option(None, "--name", help="The owner's name."),
              password_stdin: bool = typer.Option(False, "--password-stdin",
                                                  help="Read the password from standard input.")):
    """Move a laptop hub to team sign-in: the local owner becomes a password
    account with the same id, so your conversations and grants stay yours.
    Run it on the server's own machine; then set HUBZOID_AUTH=true and restart."""
    from . import users
    from .cli import _prepare

    hub = _prepare(hub)
    password = sys.stdin.readline().rstrip("\n") if password_stdin else getpass.getpass(
        "Choose a password (12 characters or more): ")
    try:
        user = users.convert_local_owner(hub, email=email, password=password, name=name)
    except (users.InvalidAccount, users.AccountExists) as exc:
        err.print(f"[red]{getattr(exc, 'message', None) or exc}[/red]")
        raise typer.Exit(4)
    except Exception as exc:  # noqa: BLE001 - passwords.PasswordRejected and store errors
        err.print(f"[red]{getattr(exc, 'message', None) or type(exc).__name__}[/red]")
        raise typer.Exit(2)
    console.print(f"The local owner is now {user['email']} (same account, id {user['id'][:8]}…). "
                  "Turn sign-in on (HUBZOID_AUTH=true) and restart, then sign in.")


@auth_app.command("logout")
def logout(server: str = _SERVER):
    """Sign this computer out (on the server too)."""
    c = _client(server)
    if c.session:
        try:
            c.request("POST", "/api/auth/logout")
        except remote.RemoteError:
            pass
    remote.forget(c.server)
    console.print(f"Signed out of {c.server}.")


@auth_app.command("whoami")
def whoami(server: str = _SERVER, as_json: bool = _JSON):
    """Who the saved session or API key signs in as."""
    data = _run(server, "GET", "/api/auth/session", as_json=as_json)
    if as_json:
        return _print(data, True)
    user = (data or {}).get("user")
    if not user:
        err.print("[red]Not signed in. Run hubzoid auth login --server ...[/red]")
        raise typer.Exit(3)
    console.print(f"{user.get('name') or user['email']} <{user['email']}>, {user['role']}, "
                  f"signed in with {user.get('method') or 'a session'}")


@keys_app.command("list")
def keys_list(server: str = _SERVER, as_json: bool = _JSON):
    data = _run(server, "GET", "/api/me/api-keys", as_json=as_json)
    if as_json:
        return _print(data, True)
    table = Table("Name", "Key", "Can", "Agents", "State")
    for k in data["api_keys"]:
        table.add_row(k["name"], k["prefix"] + "…", ", ".join(k["operations"]),
                      ", ".join(k["hubs"]) if k["hubs"] else "all", k["state"])
    console.print(table)


@keys_app.command("create")
def keys_create(name: str, server: str = _SERVER,
                preset: str = typer.Option("chat", help="chat, read or all."),
                agent: list[str] = typer.Option(None, "--agent", help="Limit to this agent (repeat)."),
                days: int = typer.Option(90, help="Days until it expires."),
                as_json: bool = _JSON):
    """Make a key. It is printed once: store it somewhere safe."""
    body = {"name": name, "preset": preset, "expires_in_days": days}
    if agent:
        body["hubs"] = list(agent)
    data = _run(server, "POST", "/api/me/api-keys", body, as_json=as_json)
    if as_json:
        return _print(data, True)
    console.print(f"Key {data['api_key']['name']!r} made. It is shown this once:")
    print(data["key"])


@keys_app.command("revoke")
def keys_revoke(key_id: str, server: str = _SERVER):
    _run(server, "DELETE", f"/api/me/api-keys/{key_id}")
    console.print("Revoked.")


@sessions_app.command("list")
def sessions_list(server: str = _SERVER, as_json: bool = _JSON):
    data = _run(server, "GET", "/api/me/sessions", as_json=as_json)
    if as_json:
        return _print(data, True)
    table = Table("Id", "Device", "Where", "This one")
    for s in data["sessions"]:
        table.add_row(s["id"], s["device"], s["place"], "yes" if s["current"] else "")
    console.print(table)


@sessions_app.command("revoke")
def sessions_revoke(session_id: str, server: str = _SERVER):
    _run(server, "DELETE", f"/api/me/sessions/{session_id}")
    console.print("Signed that device out.")


@sessions_app.command("revoke-others")
def sessions_revoke_others(server: str = _SERVER):
    data = _run(server, "POST", "/api/me/sessions/revoke-others")
    console.print(f"Signed out {data.get('ended', 0)} other device(s).")


@mfa_app.command("status")
def mfa_status(server: str = _SERVER, as_json: bool = _JSON):
    data = _run(server, "GET", "/api/me/security", as_json=as_json)
    if as_json:
        return _print({"mfa": data["mfa"], "mfa_policy": data["mfa_policy"],
                       "mfa_required": data["mfa_required"]}, True)
    on = data["mfa"]["enabled"]
    console.print(f"Two-step sign-in is {'on' if on else 'off'}"
                  + (f", {data['mfa']['recovery_codes_left']} recovery codes left" if on else "")
                  + f". Deployment rule: {data['mfa_policy']}.")


@mfa_app.command("policy")
def mfa_policy(policy: str = typer.Argument(None, help="none, admins or all. Omit to show it."),
               server: str = _SERVER):
    """Who must use two-step sign-in (administrators change it)."""
    if policy is None:
        console.print(_run(server, "GET", "/api/auth/mfa/policy")["policy"])
        return
    console.print(_run(server, "PUT", "/api/auth/mfa/policy", {"policy": policy})["policy"])


def api(method: str = typer.Argument(..., help="GET, POST, PUT, PATCH or DELETE."),
        path: str = typer.Argument(..., help="The route, for example /api/agents."),
        data: str = typer.Option(None, "--data", help="A JSON body, or @file.json."),
        server: str = _SERVER):
    """Call any Hubzoid API route as you (session or HUBZOID_API_KEY) and print its answer."""
    body = None
    if data:
        raw = Path(data[1:]).read_text() if data.startswith("@") else data
        try:
            body = json.loads(raw)
        except ValueError:
            err.print("[red]--data must be JSON.[/red]")
            raise typer.Exit(2)
    if not path.startswith("/"):
        err.print("[red]Give the route as a path, for example /api/agents.[/red]")
        raise typer.Exit(2)
    result = _run(server, method, path, body, as_json=True)
    print(json.dumps(result, indent=2, default=str))
