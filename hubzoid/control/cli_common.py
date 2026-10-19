"""What every 2.0 control-plane CLI command shares (conventions section 4).

* ``--json`` prints exactly the object the API returns, and errors as
  ``{"detail": {"code", "message", ...}}`` on stdout.
* Exit codes: 0 done, 1 the operation ran and failed, 2 invalid input or
  usage, 3 permission denied, 4 conflict, 5 a dependency is unavailable,
  6 accepted but not finished.

Local mode acts on the deployment's files and operational store as the person
at the keyboard (``cli:<os user>``), as ``hubzoid grant`` does: someone who can
write the deployment folder already controls it.
"""
from __future__ import annotations

import getpass
import json
import sys
from typing import Any, NoReturn

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)

EXIT_OK, EXIT_FAILED, EXIT_INVALID, EXIT_DENIED, EXIT_CONFLICT, EXIT_UNAVAILABLE, EXIT_PENDING = range(7)


def actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001
        return "cli:local"


def exit_for_status(status: int) -> int:
    if status in (400, 404, 413, 422):
        return EXIT_INVALID
    if status in (401, 403):
        return EXIT_DENIED
    if status == 409:
        return EXIT_CONFLICT
    if status in (503,):
        return EXIT_UNAVAILABLE
    if status == 202:
        return EXIT_PENDING
    return EXIT_FAILED


def emit(obj: Any, as_json: bool, human=None) -> None:
    if as_json:
        sys.stdout.write(json.dumps(obj, indent=2, default=str) + "\n")
        return
    if human is not None:
        human(obj)
    else:
        console.print_json(json.dumps(obj, default=str))


def fail(status: int, code: str, message: str, as_json: bool, **extra: Any) -> NoReturn:
    detail = {"code": code, "message": message, **{k: v for k, v in extra.items() if v is not None}}
    if as_json:
        sys.stdout.write(json.dumps({"detail": detail}, indent=2, default=str) + "\n")
    else:
        where = ""
        if extra.get("file") or extra.get("line"):
            where = f" ({extra.get('file') or ''}{':' + str(extra['line']) if extra.get('line') else ''})"
        err_console.print(f"[red]{code}[/red]: {message}{where}")
        if extra.get("nearest"):
            err_console.print(f"[dim]did you mean: {', '.join(extra['nearest'])}[/dim]")
    raise typer.Exit(exit_for_status(status))


def parse_expected(value: str | None) -> int | None | str:
    """``--expected-revision``: an integer, or ``absent`` (create only).
    Returns the string ``"missing"`` when not given."""
    if value is None:
        return "missing"
    if value == "absent":
        return None
    try:
        out = int(value)
    except ValueError:
        raise typer.BadParameter("use a revision number or 'absent'")
    if out < 0:
        raise typer.BadParameter("revisions are 0 or more")
    return out
