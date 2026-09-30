"""``hubzoid migrate openwebui``: move an Open WebUI install to the Hubzoid web app.
STUB: lane F implements."""
from __future__ import annotations

import typer

migrate_app = typer.Typer(
    help="Move an existing install to the Hubzoid web app (accounts, groups, chats, shares).",
    no_args_is_help=True,
)
