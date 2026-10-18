"""`hubzoid budgets ...`: usage limits from budgets.yaml.

Same rules and error codes as the API. Exit codes as `hubzoid work-items`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.markup import escape

from .work_items.cli import _fail, _hub, _operator, _out, _run, console
from .work_items.errors import WorkItemError

budgets_app = typer.Typer(no_args_is_help=True, help="Usage limits: budgets.yaml, usage and reservations.")


def _limit_line(scope: str, caps: dict) -> str:
    parts = [f"{k.replace('max_', '').replace('_', ' ')} {v}" for k, v in caps.items()]
    return f"{scope}: " + ", ".join(parts)


@budgets_app.command("show")
def show(hub: Path = typer.Argument(Path(".")), as_json: bool = typer.Option(False, "--json")) -> None:
    """The limits in force, this month's usage and reservations still open."""
    from . import budgets

    out = _run(budgets.status, as_json, _hub(hub))

    def render(v):
        if not v["present"]:
            console.print("No budgets.yaml: nothing is limited.")
        for scope, caps in v["limits"].items():
            console.print(_limit_line(scope, caps))
        m = v["month"]
        console.print(f"This month ({m['key']}): {m['input_tokens']:,} input tokens, {m['output_tokens']:,} output "
                      f"tokens, {float(m['cost_usd']):.4f} USD"
                      + (f", {m['unpriced_calls']} calls with no price" if m["unpriced_calls"] else ""))
        for r in v["open_reservations"]:
            console.print(f"  open reservation {r['id']} for {r['run_id']}"
                          + (" [yellow](unknown: its call never reported)[/yellow]" if r["unknown"] else ""))

    _out(out, as_json, render)


@budgets_app.command("validate")
def validate(hub: Path = typer.Argument(Path(".")), file: Optional[Path] = typer.Option(
                 None, "--file", help="Check this file instead of the hub's budgets.yaml."),
             as_json: bool = typer.Option(False, "--json")) -> None:
    """Check budgets.yaml (or --file). Nothing changes."""
    from . import budgets

    hub = _hub(hub)
    source = file or hub / budgets.FILE
    if not source.is_file():
        _out({"valid": True, "present": False, "limits": {}}, as_json,
             lambda v: console.print("No budgets.yaml: nothing is limited."))
        return
    cfg = _run(budgets.parse, as_json, source.read_text(encoding="utf-8"), source.name)
    _out({"valid": True, "present": True, "revision": cfg.revision, "limits": cfg.to_dict()}, as_json,
         lambda v: [console.print(_limit_line(s, c)) for s, c in v["limits"].items()]
         or console.print("Valid, with no limits."))


@budgets_app.command("apply")
def apply(hub: Path = typer.Argument(Path(".")), file: Path = typer.Option(..., "--file"),
          expected_revision: str = typer.Option(..., "--expected-revision",
                                                help="The revision `show` printed, or absent for a new file."),
          as_json: bool = typer.Option(False, "--json")) -> None:
    """Replace budgets.yaml with FILE after validating it. Running work keeps
    what it spent; the next model call is checked against the new limits."""
    from . import budgets

    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(WorkItemError("invalid_request", f"Cannot read {file}: {exc.strerror}"), as_json)
    out = _run(budgets.apply, as_json, _hub(hub), text, expected_revision=expected_revision, actor=_operator())
    _out(out, as_json, lambda v: console.print(f"Applied revision {v['revision']}."))


@budgets_app.command("release")
def release(hub: Path = typer.Argument(Path(".")), reservation: str = typer.Argument(...),
            reason: str = typer.Option(..., "--reason"), as_json: bool = typer.Option(False, "--json")) -> None:
    """Release a reservation whose call never reported, after checking the
    provider's own usage. Recorded in the audit log."""
    from . import budgets

    out = _run(budgets.release, as_json, _hub(hub), reservation, _operator(), reason=reason)
    _out(out, as_json, lambda v: console.print(f"Released {escape(v['id'])}."))
