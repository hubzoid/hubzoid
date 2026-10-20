"""``hubzoid secrets``: list, set and delete stored secrets from the terminal.

    hubzoid secrets list   [HUB] [--json] [--skip-secret-fetch]
    hubzoid secrets set    [HUB] --name NAME [--restricted | --deployment] [--stdin]
                                 [--expected-revision N|absent] [--check] [--json]
    hubzoid secrets delete [HUB] --name NAME [--deployment] [--expected-revision N]
                                 [--check] [--json]

``--ref secret:hub/<hub>/<name>`` or ``secret:deployment/<name>`` may stand in
for ``--name`` and the level flag. A value is read hidden from the terminal,
or from standard input with ``--stdin``, never from an argument, and it is
never printed. The same store as the Console and ``/api/hubs/{hub_id}/secrets``:
a saved value applies after the bridge restarts.

Exit codes: 0 done, 1 failed, 2 invalid input, 4 conflict (a changed revision,
or a name an operator source sets), 5 the store is unavailable.
"""
from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()


def hub_key(hub_dir: Path) -> str:
    from ..assertions import hub_key as key

    return key(hub_dir)

secrets_app = typer.Typer(
    help="Secrets stored for the Console: list, set and delete. Values are never shown.",
    no_args_is_help=True,
)

_EXIT = {400: 2, 401: 3, 403: 3, 404: 1, 409: 4, 413: 2, 422: 2, 503: 5}


def _fail(code: str, message: str, status: int, as_json: bool, **extra) -> None:
    if as_json:
        print(json.dumps({"detail": {"code": code, "message": message, **extra}}))
    else:
        console.print(f"[red]{escape(message)}[/red]")
    raise typer.Exit(_EXIT.get(status, 1))


def _hub(hub: Path, as_json: bool) -> Path:
    if not hub.is_dir():
        _fail("invalid_request", f"{hub} is not a hub folder.", 400, as_json)
    return hub.resolve()


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001 - no login name on this machine
        return "cli"


def _target(hub_dir: Path, name: str | None, ref: str | None, restricted: bool, deployment: bool,
            as_json: bool) -> tuple[str, str, str]:
    """(scope, target, name) from --name and the level flags, or from --ref."""
    from .refs import RefError, parse

    if ref:
        if name or deployment:
            _fail("invalid_request", "Use --ref or --name with --deployment, not both.", 400, as_json)
        try:
            parsed = parse(ref)
        except RefError as exc:
            _fail(exc.code, str(exc), 400, as_json)
        if parsed.provider != "secret" or parsed.scope == "channel":
            _fail("invalid_request", "Set a secret:hub/... or secret:deployment/... reference.", 400, as_json)
        if parsed.scope == "hub" and parsed.hub != hub_key(hub_dir):
            _fail("hub_not_found", f"{ref} names another hub. Run this in that hub's folder.", 404, as_json)
        return parsed.scope, parsed.target, parsed.name
    if not name:
        _fail("invalid_request", "Name the secret with --name (or --ref).", 400, as_json)
    if restricted and deployment:
        _fail("invalid_request", "A deployment secret cannot be restricted.", 400, as_json)
    return ("deployment", "", name) if deployment else ("hub", hub_key(hub_dir), name)


def _revision(raw: str | None, as_json: bool):
    if raw is None:
        return None
    if raw == "absent":
        return "absent"
    try:
        return int(raw)
    except ValueError:
        _fail("validation_failed", "--expected-revision is a number or absent.", 422, as_json)


def _read_value(from_stdin: bool, as_json: bool) -> str:
    if from_stdin:
        raw = sys.stdin.read()
        return raw[:-2] if raw.endswith("\r\n") else raw[:-1] if raw.endswith("\n") else raw
    if not sys.stdin.isatty():
        _fail("invalid_request", "No terminal to ask for the value. Pipe it with --stdin.", 400, as_json)
    return getpass.getpass("Value (hidden): ")


def _destination(hub_dir: Path, name: str) -> str | None:
    """Where a bound value would be sent, from this hub's layers."""
    import os

    from .. import config_secrets as cs
    from . import names

    keys = names.BINDINGS.get(name)
    if not keys:
        return None
    env = {}
    for key in keys:
        value, _layer = cs.resolve_key(hub_dir, key)
        env[key] = value if value is not None else os.environ.get(key, "")
    return names.destination(name, env)


@secrets_app.command("list")
def list_cmd(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    skip_secret_fetch: bool = typer.Option(False, "--skip-secret-fetch",
                                           help="Do not read AWS secrets (their keys are left out)."),
) -> None:
    """Every secret this hub uses or expects: level, status and source, never a value."""
    from . import report

    hub_dir = _hub(hub, as_json)
    try:
        all_rows = report.rows(hub_dir, fetch_secrets=not skip_secret_fetch)
    except Exception as exc:  # noqa: BLE001 - reported by class only
        _fail("unavailable", f"Secrets could not be listed ({type(exc).__name__}).", 503, as_json)
    if as_json:
        print(json.dumps({"hub": hub_key(hub_dir), "secrets": all_rows, "restricted_hidden": 0}, indent=2))
        return
    if not all_rows:
        console.print("[dim]No secrets set or expected for this hub.[/dim]")
        return
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    for col in ("Name", "Level", "Status", "Source", "Note"):
        table.add_column(col)
    colors = {"set": "green", "missing": "yellow", "blank": "yellow", "unreadable": "red", "rebind": "red"}
    for r in all_rows:
        note = []
        if r["stored_status"] == "overridden":
            note.append("stored value unused: an operator source sets it")
        elif r["stored_status"] in ("unreadable", "rebind"):
            note.append("set it again")
        if r["status"] == "blank":
            note.append("empty value")
        if r["status"] == "missing" and r["expected"]:
            note.append(f"needed ({r['expected']})")
        color = colors.get(r["status"], "white")
        table.add_row(r["name"], r["level"], f"[{color}]{r['status']}[/{color}]", r["source"] or "",
                      "; ".join(note))
    console.print(table)


@secrets_app.command("set")
def set_cmd(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    name: str = typer.Option(None, "--name", help="The secret's name, for example OPENAI_API_KEY."),
    ref: str = typer.Option(None, "--ref", help="Or a reference: secret:hub/<hub>/<name> or "
                                                "secret:deployment/<name>."),
    restricted: bool = typer.Option(None, "--restricted/--not-restricted",
                                    help="Only restricted tools see it, as with restricted/.env. "
                                         "Omitted: a stored secret keeps its setting."),
    deployment: bool = typer.Option(False, "--deployment", help="Shared by every hub of the deployment."),
    from_stdin: bool = typer.Option(False, "--stdin", help="Read the value from standard input."),
    expected_revision: str = typer.Option(None, "--expected-revision",
                                          help="Replace only this revision, or 'absent' to create only."),
    check: bool = typer.Option(False, "--check", help="Validate and show what would change. Writes nothing."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Store a secret. The value is asked for hidden and never shown."""
    from . import names, report, store

    hub_dir = _hub(hub, as_json)
    scope, target, secret_name = _target(hub_dir, name, ref, restricted, deployment, as_json)
    if scope in ("hub", "deployment"):
        where = "the deployment's .env" if scope == "deployment" else f"{hub_dir.name}/.env"
        _fail("operator_source", f"Set {secret_name} in {where} (or the host's environment). Hubzoid stores "
              "only keys it obtains itself, and secrets a channel file names as secret:channel/...", 409, as_json)
    why = names.problem(secret_name)
    if why:
        _fail("validation_failed", why, 422, as_json, fields={"name": "invalid"})
    from .. import config_secrets as cs

    try:
        all_rows = report.rows(hub_dir, strict=True)
    except cs.SecretFetchError as exc:
        _fail("unavailable", f"Cannot check who sets {secret_name}: {exc}", 503, as_json)
    row = next((r for r in all_rows if r["name"] == secret_name), None)
    if row and row["locked"]:
        _fail("secret_locked", row["lock_reason"] or "An operator source sets this secret.", 409, as_json)
    existing = store.get_meta(hub_dir, scope=scope, target=target, name=secret_name)
    if check:
        result = {"name": secret_name, "scope": scope, "would": "replace" if existing else "create",
                  "current_revision": existing.revision if existing else None, "changed": False}
        print(json.dumps(result)) if as_json else console.print(
            f"Would {result['would']} {scope} secret {secret_name}. Nothing was written.")
        return
    value = _read_value(from_stdin, as_json)
    try:
        meta = store.put(hub_dir, scope=scope, target=target, name=secret_name, value=value,
                         actor=_actor(), restricted=restricted,
                         destination=_destination(hub_dir, secret_name),
                         expected_revision=_revision(expected_revision, as_json), surface="cli")
    except store.SecretError as err:
        _fail(err.code, err.message, err.status, as_json, **err.extra)
    except Exception as exc:  # noqa: BLE001 - reported by class only, never the value
        _fail("unavailable", f"The secret store is unavailable ({type(exc).__name__}).", 503, as_json)
    if as_json:
        print(json.dumps({"secret": meta.public(), "restart_required": True}))
        return
    console.print(f"[green]Saved[/green] {secret_name} ({scope}, revision {meta.revision}). "
                  "It applies after the bridge restarts.")


@secrets_app.command("delete")
def delete_cmd(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    name: str = typer.Option(None, "--name", help="The secret's name."),
    ref: str = typer.Option(None, "--ref", help="Or a reference: secret:hub/<hub>/<name>."),
    deployment: bool = typer.Option(False, "--deployment", help="A deployment secret."),
    expected_revision: str = typer.Option(None, "--expected-revision", help="Remove only this revision."),
    check: bool = typer.Option(False, "--check", help="Show what would be removed. Writes nothing."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Remove a stored secret. Operator sources (.env, AWS) are never touched."""
    from . import store

    hub_dir = _hub(hub, as_json)
    scope, target, secret_name = _target(hub_dir, name, ref, False, deployment, as_json)
    if check:
        existing = store.get_meta(hub_dir, scope=scope, target=target, name=secret_name)
        if existing is None:
            _fail("not_found", f"No stored secret named {secret_name}.", 404, as_json)
        print(json.dumps({"name": secret_name, "scope": scope, "would": "delete",
                          "current_revision": existing.revision, "changed": False})) if as_json else \
            console.print(f"Would delete {scope} secret {secret_name}. Nothing was written.")
        return
    revision = _revision(expected_revision, as_json)
    if revision == "absent":
        _fail("validation_failed", "--expected-revision for delete is a number.", 422, as_json)
    try:
        meta = store.delete(hub_dir, scope=scope, target=target, name=secret_name, actor=_actor(),
                            expected_revision=revision, surface="cli")
    except store.SecretError as err:
        _fail(err.code, err.message, err.status, as_json, **err.extra)
    except Exception as exc:  # noqa: BLE001
        _fail("unavailable", f"The secret store is unavailable ({type(exc).__name__}).", 503, as_json)
    if as_json:
        print(json.dumps({"deleted": meta.public(), "restart_required": True}))
        return
    console.print(f"[green]Deleted[/green] {secret_name} ({scope}). It stops applying after the bridge restarts.")
