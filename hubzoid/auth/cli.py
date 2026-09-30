"""``hubzoid admin``: create administrators and users, reset passwords.
STUB: lane A implements the commands."""
from __future__ import annotations

import typer

admin_app = typer.Typer(
    help="Hubzoid accounts: create administrators and users, reset passwords.",
    no_args_is_help=True,
)
