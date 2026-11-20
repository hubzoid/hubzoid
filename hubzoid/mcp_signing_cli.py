"""`hubzoid mcp signing-keys ...`: the keys that sign MCP access tokens."""
from __future__ import annotations

import time
from pathlib import Path

import typer
from rich.console import Console

console = Console()
mcp_app = typer.Typer(no_args_is_help=True, help="The hosted MCP server's sign-in.")
keys_app = typer.Typer(no_args_is_help=True, help="Keys that sign MCP access tokens (JWTs).")
mcp_app.add_typer(keys_app, name="signing-keys")

HUB = typer.Option(Path("."), "--hub", help="The hub folder (any hub of the deployment).")


def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else "-"


@keys_app.command("list")
def keys_list(hub: Path = HUB) -> None:
    """Every signing key: which one signs, which still verify."""
    from . import mcp_signing

    for k in mcp_signing.list_keys(hub.resolve()):
        state = ("retired" if k["retired_at"] else "verifies only" if k["superseded_at"] else "signs")
        console.print(f"{k['kid']}  {state}  created {_when(k['created_at'])}"
                      + (f"  superseded {_when(k['superseded_at'])}" if k["superseded_at"] else "")
                      + (f"  retired {_when(k['retired_at'])}" if k["retired_at"] else ""))


@keys_app.command("rotate")
def keys_rotate(hub: Path = HUB,
                now: bool = typer.Option(False, "--now", help="Stop the old keys verifying at once "
                                         "(every connected assistant refreshes its token).")) -> None:
    """Add a new key that signs from now on. The old key keeps verifying for
    ten minutes plus clock skew, so nobody is signed out."""
    from . import mcp_signing

    try:
        kid = mcp_signing.rotate(hub.resolve(), now=now)
    except mcp_signing.SigningError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    console.print(f"New signing key {kid}. Other bridges pick it up within 30 seconds."
                  + (" Old keys no longer verify in Hubzoid. Outside verifiers that cached the public keys "
                     "may accept old tokens until those expire (ten minutes at most)." if now else ""))
