"""``hubzoid admin``: create administrators and users, reset passwords.

For the server's operator, who has the authority of the machine: these
commands work without signing in, on the deployment's shared account store
(any hub of a gateway reaches the same one). Passwords are typed at a hidden
prompt, never passed as arguments, never printed. By default a new account
and a reset get a one-time sign-in link instead (72 hours, single use).

    hubzoid admin create ana@example.com --name "Ana" --admin
    hubzoid admin create ops@example.com --owner --password
    hubzoid admin create bo@example.com --google-only
    hubzoid admin reset-password ana@example.com
    hubzoid admin set-role ana@example.com user
    hubzoid admin list
"""
from __future__ import annotations

import datetime as _dt
import getpass
import os
import socket
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

admin_app = typer.Typer(
    help="Hubzoid accounts: create administrators and users, reset passwords.",
    no_args_is_help=True,
)

console = Console()

_HUB_HELP = "Hub directory (any hub of a gateway). Default: the current directory."


def _operator() -> str:
    """Who ran the command, as recorded in Activity."""
    return f"cli:{getpass.getuser()}@{socket.gethostname()}"


def _fail(message: str, code: int = 1) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code)


def _prepare(hub: Path) -> Path:
    """The hub, with its configuration loaded as ``hubzoid run`` loads it."""
    from .. import appmode

    hub = Path(hub).resolve()
    if not (hub / "AGENTS.md").is_file() and not (hub / ".hubzoid" / "deployment.json").is_file():
        _fail(f"{hub} is not a hub (no AGENTS.md). Run this in a hub directory or pass one.", 2)
    try:
        from ..cli import _load_settings

        _load_settings(hub)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 — never print a configuration value
        _fail(f"The hub's configuration could not be loaded ({type(exc).__name__}).", 2)
    if appmode.is_openwebui(hub):
        _fail("This deployment uses Open WebUI accounts (HUBZOID_UI=openwebui). Manage people "
              "in the Console (People) or in Open WebUI.", 2)
    return hub


def _link_base() -> str:
    from .. import appmode

    return appmode.public_url() or f"http://127.0.0.1:{os.environ.get('PORT') or 3080}"


def _when(ts: float | None) -> str:
    if not ts:
        return "never"
    return _dt.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _email(raw: str) -> str:
    from .users import EMAIL_PATTERN, is_local_address, normalize_email

    email = normalize_email(raw)
    if not EMAIL_PATTERN.match(email) or len(email) > 320:
        _fail("Enter a valid email address.", 2)
    if is_local_address(email):
        _fail("A localhost address can't sign in. Use a real email address.", 2)
    return email


def _ask_password() -> str:
    from . import passwords

    value = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        return passwords.check(value)
    except passwords.PasswordRejected as exc:
        _fail(exc.message, 2)
    raise AssertionError  # unreachable


def _print_link(token: str, expires: float) -> None:
    from . import links

    console.print("One-time sign-in link (share it with them directly; it works once):")
    console.print(f"  {links.url(token, _link_base())}", soft_wrap=True, highlight=False)
    console.print(f"Expires {_when(expires)}.")


def _store_url(hub: Path) -> str:
    from sqlalchemy.engine import make_url

    from ..db import operational_url

    return make_url(operational_url(hub)).render_as_string(hide_password=True)


@admin_app.command("create")
def create(
    email: str = typer.Argument(..., help="Their email address. They sign in with it."),
    hub: Path = typer.Argument(Path("."), help=_HUB_HELP),
    name: str = typer.Option("", "--name", help="Their name. Default: the part before @."),
    admin: bool = typer.Option(False, "--admin", help="Make them an Administrator."),
    owner: bool = typer.Option(
        False, "--owner",
        help="Give them the owner's access on every hub of the deployment (implies --admin)."),
    password: bool = typer.Option(
        False, "--password/--link",
        help="Type their password now, or print a one-time sign-in link (the default)."),
    google_only: bool = typer.Option(
        False, "--google-only", help="They sign in with Google only: no password, no link."),
) -> None:
    """Create an account, optionally an Administrator or the deployment's owner."""
    from ..access import store_for
    from ..access.store import MANAGE_ACCESS, ORG, USE_HUB
    from . import links, oidc, users

    hub = _prepare(hub)
    email = _email(email)
    if google_only and password:
        _fail("Choose --google-only or --password, not both.", 2)
    console.print(f"Account store: {_store_url(hub)}")
    gs = store_for(hub)
    with gs.engine.connect() as conn:
        blocked = gs._meta_get(conn, "suspended:" + email) == "1"  # noqa: SLF001
    if blocked:
        _fail(f"{email} is blocked. Reactivate them in the Console (People) first.")
    st = users.store(hub)
    if st.find_by_email(email) is not None:
        _fail(f"An account for {email} already exists. Use `hubzoid admin reset-password` or "
              "`hubzoid admin set-role` to change it.")
    if google_only and not (oidc.provider("google") and oidc.merge_by_email()):
        console.print("[yellow]Google sign-in isn't set up here with "
                      "OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true, so this account can't sign in until "
                      "it is.[/yellow]")
    secret = _ask_password() if password else None
    role = "admin" if (admin or owner) else "user"
    display = name.strip() or email.split("@", 1)[0]
    try:
        user = st.create(email=email, name=display, role=role, status="active",
                         password=secret, password_enabled=not google_only, source="admin")
    except users.AccountExists:
        _fail(f"An account for {email} already exists.")
    except users.InvalidAccount as exc:
        _fail(exc.message, 2)
    secret = None  # noqa: F841 — not kept past the hash
    operator = _operator()
    recorded = (gs.identity(email) or {}).get("owui_id")
    replaced = bool(recorded and recorded != user["id"])
    try:
        gs.bind_new_account(email, owui_id=user["id"], display=display, grants={},
                            actor=operator, replace=replaced, surface=None)
    except ValueError as exc:
        _fail(f"The account was created, but it could not be recorded for access: {exc}")
    if replaced:
        console.print("[yellow]This email belonged to an earlier account. Its access was "
                      "removed; give access again in the Console.[/yellow]")
    if role == "admin":
        gs.grant(email, ORG, MANAGE_ACCESS, actor=operator)
    kind = "administrator" if role == "admin" else "user"
    console.print(f"[green]Created[/green] {kind} {email}.")
    if owner:
        provisioned = set(users.provision_owner(hub, email))
        from .. import deployment

        for h in deployment.hubs(hub):
            if h["key"] not in provisioned:
                store_for(Path(h["path"])).grant(email, h["key"], USE_HUB, actor=operator)
        hubs = ", ".join(sorted(h["key"] for h in deployment.hubs(hub)))
        console.print(f"Owner access on: {hubs}.")
    if google_only:
        console.print(f"They sign in with Google as {email}.")
    elif not password:
        token, expires = links.create(hub, user["id"], purpose="set_password",
                                      created_by=operator)
        _print_link(token, expires)
    else:
        console.print("They sign in with the password you typed.")


@admin_app.command("reset-password")
def reset_password(
    email: str = typer.Argument(..., help="Their email address."),
    hub: Path = typer.Argument(Path("."), help=_HUB_HELP),
    password: bool = typer.Option(
        False, "--password", help="Type the new password now instead of printing a link."),
) -> None:
    """Reset a password. By default the current password stops working, their
    sessions end, and a one-time link to set a new one is printed."""
    from ..access import store_for
    from ..access.store import ORG
    from . import links, users

    hub = _prepare(hub)
    email = _email(email)
    st = users.store(hub)
    user = st.find_by_email(email)
    if user is None:
        _fail(f"No account uses {email}.")
    if not user["password_enabled"]:
        _fail(f"{email} signs in with Google only, so there is no password to reset.")
    operator = _operator()
    if password:
        st.set_password(user["id"], _ask_password())
        console.print(f"[green]Password set[/green] for {email}. Their sessions were signed out.")
    else:
        st.set_password(user["id"], None)
        token, expires = links.create(hub, user["id"], purpose="reset_password",
                                      created_by=operator)
        console.print(f"[green]Password reset[/green] for {email}. Their sessions were signed "
                      "out and the old password no longer works.")
        _print_link(token, expires)
    store_for(hub).audit_event(operator, "account_password_reset", subject=email, hub=ORG)


@admin_app.command("list")
def list_accounts(hub: Path = typer.Argument(Path("."), help=_HUB_HELP)) -> None:
    """List every account: role, status, how they sign in, last sign-in."""
    from ..access import store_for
    from . import users

    hub = _prepare(hub)
    gs = store_for(hub)
    people = users.list_users(hub)
    table = Table(show_header=True, header_style="bold")
    for column in ("Email", "Name", "Role", "Status", "Signs in with", "Last sign-in"):
        table.add_column(column, overflow="fold")
    for u in people:
        with gs.engine.connect() as conn:
            blocked = gs._meta_get(conn, "suspended:" + u["email"]) == "1"  # noqa: SLF001
        status = "blocked" if blocked else u["status"]
        how = users.sign_in_of(u) if u["source"] != "local" else "(local owner)"
        if how == "password" and not u["has_password"]:
            how = "password (not set yet)"
        table.add_row(u["email"], u["name"], "Administrator" if u["role"] == "admin" else "User",
                      status, how, _when(u["last_login_at"]))
    console.print(table)
    console.print(f"{len(people)} account{'s' if len(people) != 1 else ''}.")


@admin_app.command("set-role")
def set_role(
    email: str = typer.Argument(..., help="Their email address."),
    role: str = typer.Argument(..., help="admin or user."),
    hub: Path = typer.Argument(Path("."), help=_HUB_HELP),
) -> None:
    """Make someone an Administrator (admin) or a User (user). Sets Hubzoid
    administration and the web app's admin role together, and signs them out
    so the change applies at once. Approves an account awaiting approval."""
    from ..access import store_for
    from ..access.store import MANAGE_ACCESS, ORG, LastAdminError
    from . import users

    role = role.strip().lower()
    if role not in users.ROLES:
        _fail("Choose admin or user.", 2)
    hub = _prepare(hub)
    email = _email(email)
    st = users.store(hub)
    user = st.find_by_email(email)
    if user is None:
        _fail(f"No account uses {email}.")
    gs = store_for(hub)
    console_admin = any(s == email and p == MANAGE_ACCESS for (s, _h, p) in gs.list_grants(ORG))
    operator = _operator()
    if role == "admin":
        with gs.engine.connect() as conn:
            blocked = gs._meta_get(conn, "suspended:" + email) == "1"  # noqa: SLF001
        if blocked:
            _fail(f"{email} is blocked, so they can't be made an administrator.")
    else:
        others = [a for a in users.admins(hub) if a["email"] != email]
        org_others = [s for (s, _h, p) in gs.list_grants(ORG)
                      if p == MANAGE_ACCESS and s not in (email, "*")]
        if (user["role"] == "admin" and not others) or (console_admin and not org_others):
            _fail("This is the last administrator. Make someone else an administrator first.")
    signed_out = st.set_role(user["id"], role)
    changed = signed_out
    if user["status"] == "pending":
        st.set_status(user["id"], "active")
        users.sync_identity(hub, st.get(user["id"]) or user)
        changed = True
        console.print(f"Approved {email}.")
    try:
        if role == "admin" and not console_admin:
            gs.grant(email, ORG, MANAGE_ACCESS, actor=operator)
            changed = True
        elif role == "user" and console_admin:
            gs.revoke(email, ORG, MANAGE_ACCESS, actor=operator)
            changed = True
    except LastAdminError:
        _fail("This is the last administrator. Make someone else an administrator first.")
    label = "an Administrator" if role == "admin" else "a User"
    if not changed:
        console.print(f"{email} is already {label}.")
        return
    gs.audit_event(operator, "account_role", subject=email, hub=ORG, permission=role)
    console.print(f"[green]{email}[/green] is now {label}."
                  + (" Their sessions were signed out." if signed_out else ""))
