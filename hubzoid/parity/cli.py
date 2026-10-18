"""``hubzoid parity validate|coverage|ineffective|candidate|review|freeze|diff``.

Pure validation runs offline on the files. Candidates, reviews and freezes go
to the deployment's operational store. A review made here is recorded as
signed by the operator at the keyboard (they control the local deployment).
"""
from __future__ import annotations

from pathlib import Path

import typer

from ..control import cli_common as cc
from . import store
from .validate import ParityError, check, ineffective, load

parity_app = typer.Typer(help="The chat parity ledger: validate, coverage, candidate, review, freeze.",
                         no_args_is_help=True)
HUB = typer.Argument(Path("."), help="Hub folder (its deployment folder holds parity.yaml).")
FILE = typer.Option(Path("parity.yaml"), "--file", help="The manifest, relative to the deployment folder.")
JSON = typer.Option(False, "--json")


class _guard:
    def __init__(self, as_json):
        self.as_json = as_json

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if isinstance(exc, ParityError):
            cc.fail(exc.status, exc.code, exc.message, self.as_json, **exc.extra)
        return False


def _root(hub: Path) -> Path:
    from ..control import deploy

    return deploy.root(Path(hub))


def _human(r):
    c = r["counts"]
    cc.console.print(f"{r['status']}: {c['obligations']} obligation(s), {c['acceptance_cases']} acceptance case(s), "
                     f"{c['unmapped_locators']} unmapped locator(s), {len(r['errors'])} error(s), "
                     f"{len(r['blockers'])} blocker(s)")
    cc.console.print(f"digest {r['digest']}")
    for e in r["errors"][:20]:
        cc.console.print(f"  [red]{e['code']}[/red] {e['message']}")
    for b in r["blockers"][:20]:
        cc.console.print(f"  [yellow]{b['code']}[/yellow] {b['message']}")
    cc.console.print(f"[dim]{r['note']}[/dim]")


@parity_app.command("validate")
def validate(hub: Path = HUB, file: Path = FILE, source_root: Path = typer.Option(None, "--source-root"),
             as_json: bool = JSON) -> None:
    """Check the ledger, sources, providers and inventories."""
    with _guard(as_json):
        report = check(load(_root(hub), file), source_root=source_root)
    cc.emit(report, as_json, _human)
    if not report["valid"]:
        raise typer.Exit(cc.EXIT_INVALID)
    if report["blockers"]:
        raise typer.Exit(cc.EXIT_PENDING)


@parity_app.command("coverage")
def coverage(hub: Path = HUB, file: Path = FILE, as_json: bool = JSON) -> None:
    """Counts per owner and every unmapped locator."""
    with _guard(as_json):
        report = check(load(_root(hub), file))
    cc.emit({"counts": report["counts"], "gaps": [b for b in report["blockers"] if b["code"] == "coverage_gap"],
             "digest": report["digest"]}, as_json)


@parity_app.command("ineffective")
def ineffective_cmd(hub: Path = HUB, file: Path = FILE, as_json: bool = JSON) -> None:
    """Settings whose intended effect differs from what was observed."""
    with _guard(as_json):
        rows = ineffective(load(_root(hub), file))
    cc.emit({"items": rows}, as_json)


@parity_app.command("candidate")
def candidate(hub: Path = HUB, file: Path = FILE, base: str = typer.Option(None, "--base"),
              idempotency_key: str = typer.Option(None, "--idempotency-key"), as_json: bool = JSON) -> None:
    """Record the current files as a candidate for review."""
    with _guard(as_json):
        out = store.service_for(hub).create(load(_root(hub), file), actor=cc.actor(), base_id=base,
                                            idempotency_key=idempotency_key)
    cc.emit(out, as_json)
    if out["status"] == "blocked":
        raise typer.Exit(cc.EXIT_PENDING)


@parity_app.command("review")
def review(candidate_id: str, hub: Path = HUB, digest: str = typer.Option(..., "--digest"),
           decision: str = typer.Option(..., "--decision"), reason: str = typer.Option(None, "--reason"),
           expected_revision: int = typer.Option(1, "--expected-revision"), as_json: bool = JSON) -> None:
    with _guard(as_json):
        cc.emit(store.service_for(hub).review(candidate_id, digest=digest, expected_revision=expected_revision,
                                              decision=decision, reason=reason, actor=cc.actor()), as_json)


@parity_app.command("freeze")
def freeze(candidate_id: str, hub: Path = HUB, digest: str = typer.Option(..., "--digest"),
           review: str = typer.Option(..., "--review"), as_json: bool = JSON) -> None:
    with _guard(as_json):
        cc.emit(store.service_for(hub).freeze(candidate_id, digest=digest, review_id=review, actor=cc.actor()),
                as_json)


@parity_app.command("diff")
def diff(hub: Path = HUB, base: str = typer.Option(..., "--base"), candidate: str = typer.Option(..., "--candidate"),
         as_json: bool = JSON) -> None:
    with _guard(as_json):
        cc.emit(store.service_for(hub).diff(base, candidate), as_json)


@parity_app.command("list")
def list_(hub: Path = HUB, as_json: bool = JSON) -> None:
    cc.emit({"items": store.service_for(hub).list()}, as_json)
